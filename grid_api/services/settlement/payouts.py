# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Custodial worker payouts (v1) — pay each EARNING ACCOUNT its pro-rata share of
a fixed per-period AIPG budget, by den, via direct ERC-20 transfers on Base.

Attribution is by ACCOUNT, not by a worker-supplied wallet string: a worker
authenticates with its account key, so its den belongs to the account. An account
with a `payout_wallet` (fallback login `wallet`) is paid now; one without is
recorded **accrued** (owed) and paid the moment it sets a wallet — nothing
strands. Same source of truth (grid_ledger den) as the future trustless on-chain
Merkle-claim rail (settlement/bot.py, still a scaffold).

Idempotent per (period_id, account_id) via grid_payouts. DRY-RUN by default;
`--send` executes transfers (needs SETTLEMENT_TREASURY_PK + BASE_RPC_URL).
"""

import argparse
import asyncio
import datetime as _dt
import logging
import os

import sqlalchemy as sa

from ...database import close_database, init_database, new_session
from ...v2.schema import accounts as accounts_t
from ...v2.schema import payouts as payouts_t
from .aggregate import aggregate_den_by_account, total_den_in_window

logger = logging.getLogger("grid_api.payouts")

AIPG_TOKEN_ADDRESS = os.getenv("AIPG_TOKEN_ADDRESS", "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608")
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "")
TREASURY_PK = os.getenv("SETTLEMENT_TREASURY_PK", "")  # funded AIPG sender; never logged
MIN_AIPG = float(os.getenv("PAYOUT_MIN_AIPG", "0.01"))

_ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


# ── pure math (no I/O) ───────────────────────────────────────────────────────

def compute_account_payouts(rows: list[dict], budget_aipg: float, *, min_aipg: float = MIN_AIPG) -> list[dict]:
    """Split `budget_aipg` across accounts pro-rata by den. Returns
    [{account_id, payout_address, den, share, aipg, payable}] sorted high→low,
    dropping sub-dust rows. `payable` = the account has a wallet (else it accrues).
    Emissions-funded bootstrap → the whole budget goes to supply (the 85/3/12
    revenue split is a revenue concept, not applicable to emissions)."""
    total_den = sum(float(r["den"]) for r in rows)
    if total_den <= 0 or budget_aipg <= 0:
        return []
    out = []
    for r in rows:
        share = float(r["den"]) / total_den
        aipg = budget_aipg * share
        if aipg < min_aipg:
            continue
        addr = r.get("payout_address")
        out.append({"account_id": r["account_id"], "payout_address": addr,
                    "den": float(r["den"]), "share": share, "aipg": round(aipg, 8),
                    "payable": bool(addr)})
    return sorted(out, key=lambda x: x["aipg"], reverse=True)


# ── period window ────────────────────────────────────────────────────────────

def _window(days, since, until):
    now = _now()
    end = _dt.datetime.fromisoformat(until) if until else now
    start = _dt.datetime.fromisoformat(since) if since else end - _dt.timedelta(days=days if days is not None else 1.0)
    return start, end, f"{start.date().isoformat()}_{end.date().isoformat()}"


# ── dry-run preview ──────────────────────────────────────────────────────────

async def preview_period(start, end, budget_aipg: float) -> dict:
    rows = await aggregate_den_by_account(start, end)
    pay = compute_account_payouts(rows, budget_aipg)
    attributed = sum(float(r["den"]) for r in rows)
    no_account_den = round(max(0.0, await total_den_in_window(start, end) - attributed), 2)
    payable = [p for p in pay if p["payable"]]
    accrued = [p for p in pay if not p["payable"]]
    return {
        "accounts": len(rows),
        "total_den": attributed,
        "budget_aipg": budget_aipg,
        "payouts": pay,
        "payable_now_aipg": round(sum(p["aipg"] for p in payable), 4), "n_payable": len(payable),
        "accrued_aipg": round(sum(p["aipg"] for p in accrued), 4), "n_accrued": len(accrued),
        "no_account_den": no_account_den,   # truly unattributable (no account at all)
    }


# ── persistence (idempotent per period+account) ──────────────────────────────

async def _status(period_id, account_id) -> str | None:
    async with await new_session() as s:
        row = (await s.execute(sa.select(payouts_t.c.status).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id))).first()
        return row[0] if row else None


async def _write(period_id, account_id, *, address, den, aipg, status, tx_hash=None, paid=False):
    async with await new_session() as s:
        existing = (await s.execute(sa.select(payouts_t.c.id).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id))).first()
        vals = dict(address=address, den=den, aipg_amount=aipg, status=status, tx_hash=tx_hash)
        if paid:
            vals["paid"] = _now()
        if existing:
            await s.execute(sa.update(payouts_t).where(payouts_t.c.id == existing[0]).values(**vals))
        else:
            await s.execute(sa.insert(payouts_t).values(
                period_id=period_id, account_id=account_id, created=_now(), **vals))
        await s.commit()


# ── send (gated, idempotent) ─────────────────────────────────────────────────

def _w3():
    if not (BASE_RPC_URL and TREASURY_PK):
        raise RuntimeError("SETTLEMENT_TREASURY_PK + BASE_RPC_URL required to send")
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    acct = w3.eth.account.from_key(TREASURY_PK)
    token = w3.eth.contract(address=Web3.to_checksum_address(AIPG_TOKEN_ADDRESS), abi=_ERC20_ABI)
    return Web3, w3, acct, token


async def _transfer(Web3, w3, acct, token, decimals, to_addr, aipg, nonce) -> str:
    amount_wei = int(round(aipg * (10 ** decimals)))
    # Derive maxFeePerGas from the LIVE base fee with headroom, never a fixed cap:
    # a static 0.1 gwei cap got rejected ("max fee per gas less than block base
    # fee") during a Base fee spike. 3x base + tip survives spikes; Base base fees
    # are tiny so this stays a fraction of a cent per transfer.
    priority = w3.to_wei(0.01, "gwei")
    try:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    except Exception:
        base_fee = w3.to_wei(0.1, "gwei")
    max_fee = base_fee * 3 + priority
    tx = token.functions.transfer(Web3.to_checksum_address(to_addr), amount_wei).build_transaction({
        "from": acct.address, "nonce": nonce,
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority,
    })
    signed = acct.sign_transaction(tx)
    raw = w3.eth.send_raw_transaction(signed.raw_transaction)
    # send_raw_transaction returns on BROADCAST, not inclusion. Wait for the
    # receipt so a dropped/underpriced tx is never recorded 'sent' with a hash
    # that never lands (BaseScan "tx not found"). Raise on timeout/revert so the
    # caller marks it 'failed' (retryable) instead of phantom-sent.
    receipt = w3.eth.wait_for_transaction_receipt(raw, timeout=180, poll_latency=2)
    if receipt.get("status") != 1:
        raise RuntimeError(f"payout tx reverted on-chain: {raw.hex()}")
    return raw.hex()


async def send_period(start, end, budget_aipg: float, period_id: str) -> dict:
    """Pay accounts with a wallet; record the rest as 'accrued'. Idempotent."""
    rows = await aggregate_den_by_account(start, end)
    pay = compute_account_payouts(rows, budget_aipg)
    sent = accrued = skipped = failed = 0
    Web3 = w3 = acct = token = decimals = None
    if any(p["payable"] for p in pay) and (BASE_RPC_URL and TREASURY_PK):
        Web3, w3, acct, token = _w3()
        decimals = token.functions.decimals().call()
        nonce = w3.eth.get_transaction_count(acct.address)
    for p in pay:
        st = await _status(period_id, p["account_id"])
        if st in ("sent", "confirmed"):
            skipped += 1
            continue
        if not p["payable"]:
            await _write(period_id, p["account_id"], address=None, den=p["den"],
                         aipg=p["aipg"], status="accrued")
            accrued += 1
            continue
        if w3 is None:
            failed += 1  # has a wallet but no treasury configured → can't send
            continue
        try:
            txh = await _transfer(Web3, w3, acct, token, decimals, p["payout_address"], p["aipg"], nonce)
            nonce += 1
            await _write(period_id, p["account_id"], address=p["payout_address"], den=p["den"],
                         aipg=p["aipg"], status="sent", tx_hash=txh, paid=True)
            sent += 1
        except Exception as e:
            await _write(period_id, p["account_id"], address=p["payout_address"], den=p["den"],
                         aipg=p["aipg"], status="failed", tx_hash=str(e)[:60])
            logger.error("payout failed account=%s: %s", p["account_id"], e)
            failed += 1
    return {"period_id": period_id, "sent": sent, "accrued": accrued, "skipped": skipped, "failed": failed}


async def pay_accrued() -> dict:
    """Pay every 'accrued' balance whose account NOW has a wallet — the 'pay later'
    path. Run after operators connect a payout wallet in the console."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.id, payouts_t.c.account_id, payouts_t.c.aipg_amount,
                      sa.func.coalesce(sa.func.nullif(accounts_t.c.payout_wallet, ""),
                                       sa.func.nullif(accounts_t.c.wallet, "")).label("addr"))
            .select_from(payouts_t.join(accounts_t, accounts_t.c.id == payouts_t.c.account_id))
            .where(payouts_t.c.status == "accrued")
        )).all()
    targets = [r for r in rows if r.addr]
    if not targets:
        return {"paid": 0, "still_accrued": len(rows)}
    Web3, w3, acct, token = _w3()
    decimals = token.functions.decimals().call()
    nonce = w3.eth.get_transaction_count(acct.address)
    paid = 0
    for r in targets:
        try:
            txh = await _transfer(Web3, w3, acct, token, decimals, r.addr, float(r.aipg_amount), nonce)
            nonce += 1
            async with await new_session() as s:
                await s.execute(sa.update(payouts_t).where(payouts_t.c.id == r.id)
                                .values(status="sent", address=r.addr, tx_hash=txh, paid=_now()))
                await s.commit()
            paid += 1
        except Exception as e:
            logger.error("pay_accrued failed id=%s: %s", r.id, e)
    return {"paid": paid, "still_accrued": len(rows) - paid}


async def retry_failed() -> dict:
    """Re-send payouts stuck in 'failed' (a dropped/underpriced/reverted tx). Pays
    the recorded address+amount; with the receipt-confirmed _transfer a row only
    leaves 'failed' once the tx actually mines — so this self-heals transient
    on-chain failures without a manual per-period re-run."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.period_id, payouts_t.c.account_id, payouts_t.c.address,
                      payouts_t.c.aipg_amount, payouts_t.c.den)
            .where(payouts_t.c.status == "failed", payouts_t.c.address.isnot(None))
        )).all()
    if not rows:
        return {"retried": 0, "still_failed": 0}
    Web3, w3, acct, token = _w3()
    decimals = token.functions.decimals().call()
    nonce = w3.eth.get_transaction_count(acct.address)
    sent = 0
    for r in rows:
        try:
            txh = await _transfer(Web3, w3, acct, token, decimals, r.address, float(r.aipg_amount), nonce)
            nonce += 1
            await _write(r.period_id, r.account_id, address=r.address, den=float(r.den or 0),
                         aipg=float(r.aipg_amount), status="sent", tx_hash=txh, paid=True)
            sent += 1
        except Exception as e:
            await _write(r.period_id, r.account_id, address=r.address, den=float(r.den or 0),
                         aipg=float(r.aipg_amount), status="failed", tx_hash=str(e)[:60])
            logger.error("retry_failed %s/%s: %s", r.period_id, r.account_id, e)
    return {"retried": sent, "still_failed": len(rows) - sent}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_preview(pv, period_id):
    print(f"\n=== payout preview — period {period_id} ===")
    print(f"accounts={pv['accounts']}  total_den={pv['total_den']:.2f}  budget={pv['budget_aipg']:.2f} AIPG")
    print(f"payable now: {pv['payable_now_aipg']:.4f} AIPG to {pv['n_payable']} acct(s) | "
          f"ACCRUED (no wallet yet): {pv['accrued_aipg']:.4f} AIPG owed to {pv['n_accrued']} acct(s)")
    if pv.get("no_account_den"):
        print(f"den with NO account at all (unattributable): {pv['no_account_den']:.2f}")
    print(f"{'account_id':38}{'den':>12}{'share':>9}{'AIPG':>14}  wallet")
    for p in pv["payouts"]:
        tag = (p['payout_address'][:10] + '…') if p['payable'] else 'ACCRUED (set wallet)'
        print(f"{p['account_id']:38}{p['den']:>12.2f}{p['share']*100:>8.2f}%{p['aipg']:>14.4f}  {tag}")
    print()


async def _amain():
    ap = argparse.ArgumentParser(description="Custodial AIPG worker payouts (account-based, dry-run by default).")
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--period-id")
    ap.add_argument("--budget", type=float, help="total AIPG to distribute this period")
    ap.add_argument("--send", action="store_true", help="EXECUTE transfers (default: dry-run)")
    ap.add_argument("--pay-accrued", action="store_true", help="pay all accrued balances whose account now has a wallet")
    ap.add_argument("--retry-failed", action="store_true", help="re-send payouts stuck in 'failed' (dropped/underpriced tx)")
    a = ap.parse_args()
    await init_database()
    try:
        if a.retry_failed:
            print("retrying failed payouts ...", await retry_failed()); return
        if a.pay_accrued:
            print("paying accrued balances ...", await pay_accrued()); return
        if a.budget is None:
            ap.error("--budget is required (or use --pay-accrued)")
        start, end, pid = _window(a.days, a.since, a.until)
        if a.period_id:
            pid = a.period_id
        _print_preview(await preview_period(start, end, a.budget), pid)
        if a.send:
            print(f"SENDING for {pid} ...", await send_period(start, end, a.budget, pid))
        else:
            print("(dry-run — re-run with --send to execute)")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(_amain())
