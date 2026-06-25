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

async def _row(period_id, account_id) -> dict | None:
    async with await new_session() as s:
        r = (await s.execute(sa.select(payouts_t.c.status, payouts_t.c.nonce).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id))).first()
        return {"status": r[0], "nonce": r[1]} if r else None


async def _max_assigned_nonce() -> int:
    """Highest treasury nonce ever bound to a payout. Fresh assignments go above
    this so a new payment can't collide with one already in flight even when the
    chain's pending-nonce view is stale (e.g. during a Base outage)."""
    async with await new_session() as s:
        v = (await s.execute(sa.select(sa.func.max(payouts_t.c.nonce)))).scalar()
    return int(v) if v is not None else -1


async def _write(period_id, account_id, *, address, den, aipg, status,
                 tx_hash=None, nonce=None, paid=False, set_tx=True):
    """Upsert a payout row. set_tx=False preserves the existing tx_hash (used when
    marking sent via the nonce check, where the winning hash may differ)."""
    async with await new_session() as s:
        existing = (await s.execute(sa.select(payouts_t.c.id).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id))).first()
        vals = dict(address=address, den=den, aipg_amount=aipg, status=status)
        if set_tx:
            vals["tx_hash"] = tx_hash
        if nonce is not None:
            vals["nonce"] = nonce
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


def _ctx():
    """(Web3, w3, acct, token, decimals) — the on-chain handle bundle for sends."""
    Web3, w3, acct, token = _w3()
    return (Web3, w3, acct, token, token.functions.decimals().call())


def _signed_transfer(Web3, w3, acct, token, decimals, to_addr, aipg, nonce, attempt):
    amount_wei = int(round(aipg * (10 ** decimals)))
    # Tip escalates per attempt so a retry actually REPLACES a stuck tx at the
    # same nonce; maxFee derives from the live base fee so a Base spike can't
    # reject it. Base base fees are tiny → still sub-cent.
    priority = w3.to_wei(0.05 * (2 ** min(attempt, 4)), "gwei")
    try:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    except Exception:
        base_fee = w3.to_wei(0.1, "gwei")
    tx = token.functions.transfer(Web3.to_checksum_address(to_addr), amount_wei).build_transaction({
        "from": acct.address, "nonce": nonce, "gas": 120000,
        "maxFeePerGas": base_fee * 5 + priority, "maxPriorityFeePerGas": priority,
    })
    return acct.sign_transaction(tx)


async def _settle_one(ctx, *, period_id, account_id, address, den, aipg,
                      stored_nonce, attempt=0) -> str:
    """Idempotently settle ONE (period, account) payout. Double-pay-proof: the
    payout is BOUND to a nonce — if that nonce has already mined, the payment
    landed and we never re-send; otherwise we (re)broadcast AT THE BOUND NONCE
    (a replacement), never a new one. A receipt timeout stays 'pending' (unknown)
    and is resolved next run by the nonce check. Returns 'sent'|'pending'|'failed'.
    (Funded-wallet ERC-20 transfers don't revert, so 'nonce mined' ⇒ paid.)"""
    Web3, w3, acct, token, decimals = ctx
    mined = w3.eth.get_transaction_count(acct.address)  # 'latest' = mined count

    # (1) Bound nonce already mined → this payment landed. Never re-send.
    if stored_nonce is not None and mined > stored_nonce:
        await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                     status="sent", nonce=stored_nonce, paid=True, set_tx=False)
        return "sent"

    # (2) Nonce to use: reuse the bound one (replacement) or assign a fresh,
    # collision-proof one (above both the chain's pending view and any nonce we've
    # ever assigned — survives a stale mempool during an outage).
    if stored_nonce is not None:
        nonce = stored_nonce
    else:
        nonce = max(w3.eth.get_transaction_count(acct.address, "pending"),
                    (await _max_assigned_nonce()) + 1)

    signed = _signed_transfer(Web3, w3, acct, token, decimals, address, aipg, nonce, attempt)
    h = signed.hash  # HexBytes — used for the RPC receipt wait

    # (3) Record pending + the BOUND nonce BEFORE broadcast (crash-safe: a crash
    # mid-send still leaves the nonce recorded so reconcile can resolve it).
    await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                 status="pending", tx_hash=h.hex(), nonce=nonce)

    # (4) Broadcast. "already known"/"nonce too low"/"replacement underpriced" are
    # NOT errors — they mean a tx for this nonce is already in flight or mined;
    # the nonce check resolves it. Any other error → failed (retryable).
    try:
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        msg = str(e).lower()
        if not any(k in msg for k in ("already known", "nonce too low",
                                      "replacement transaction underpriced")):
            await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                         status="failed", tx_hash=str(e)[:80], nonce=nonce)
            return "failed"

    # (5) Confirm (short). mined→sent, revert→failed, timeout→stay 'pending'.
    try:
        rec = w3.eth.wait_for_transaction_receipt(h, timeout=90, poll_latency=2)
        ok = rec.get("status") == 1
        await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                     status="sent" if ok else "failed", tx_hash=h.hex(), nonce=nonce, paid=ok)
        return "sent" if ok else "failed"
    except Exception:
        return "pending"  # UNKNOWN — not failed. The nonce check settles it next run.


async def send_period(start, end, budget_aipg: float, period_id: str) -> dict:
    """Pay accounts with a wallet (nonce-bound, idempotent); record the rest as
    'accrued'. Safe to re-run — an already-settled payout is detected via its
    bound nonce, so re-running never double-pays."""
    rows = await aggregate_den_by_account(start, end)
    pay = compute_account_payouts(rows, budget_aipg)
    counts = {"sent": 0, "pending": 0, "accrued": 0, "skipped": 0, "failed": 0}
    ctx = _ctx() if (any(p["payable"] for p in pay) and BASE_RPC_URL and TREASURY_PK) else None
    for p in pay:
        existing = await _row(period_id, p["account_id"])
        if existing and existing["status"] in ("sent", "confirmed"):
            counts["skipped"] += 1
            continue
        if not p["payable"]:
            await _write(period_id, p["account_id"], address=None, den=p["den"],
                         aipg=p["aipg"], status="accrued")
            counts["accrued"] += 1
            continue
        if ctx is None:
            counts["failed"] += 1  # has a wallet but no treasury configured → can't send
            continue
        try:
            st = await _settle_one(ctx, period_id=period_id, account_id=p["account_id"],
                                   address=p["payout_address"], den=p["den"], aipg=p["aipg"],
                                   stored_nonce=(existing or {}).get("nonce"))
            counts[st] = counts.get(st, 0) + 1
        except Exception as e:
            logger.error("payout error account=%s: %s", p["account_id"], e)
            counts["failed"] += 1
    return {"period_id": period_id, **counts}


async def pay_accrued() -> dict:
    """Pay every 'accrued' balance whose account NOW has a wallet — the 'pay later'
    path. Run after operators connect a payout wallet in the console."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.period_id, payouts_t.c.account_id, payouts_t.c.den,
                      payouts_t.c.aipg_amount, payouts_t.c.nonce,
                      sa.func.coalesce(sa.func.nullif(accounts_t.c.payout_wallet, ""),
                                       sa.func.nullif(accounts_t.c.wallet, "")).label("addr"))
            .select_from(payouts_t.join(accounts_t, accounts_t.c.id == payouts_t.c.account_id))
            .where(payouts_t.c.status == "accrued")
        )).all()
    targets = [r for r in rows if r.addr]
    if not targets:
        return {"paid": 0, "still_accrued": len(rows)}
    ctx = _ctx()
    paid = 0
    for r in targets:
        try:
            st = await _settle_one(ctx, period_id=r.period_id, account_id=r.account_id,
                                   address=r.addr, den=float(r.den or 0),
                                   aipg=float(r.aipg_amount), stored_nonce=r.nonce)
            if st == "sent":
                paid += 1
        except Exception as e:
            logger.error("pay_accrued error %s/%s: %s", r.period_id, r.account_id, e)
    return {"paid": paid, "still_accrued": len(rows) - paid}


async def reconcile_and_retry() -> dict:
    """Resolve in-flight ('pending') and genuinely-failed payouts idempotently.
    Each row runs through _settle_one, which FIRST checks whether its bound nonce
    already mined (→ settle, no re-send) and otherwise replaces at that same nonce.
    This is what makes a chain outage / dropped tx unable to double-pay: a payout
    is never re-sent at a new nonce once it has a bound one. Safe to run every hour."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.period_id, payouts_t.c.account_id, payouts_t.c.address,
                      payouts_t.c.aipg_amount, payouts_t.c.den, payouts_t.c.nonce)
            .where(payouts_t.c.status.in_(("pending", "failed")), payouts_t.c.address.isnot(None))
        )).all()
    if not rows:
        return {"settled": 0, "pending": 0, "failed": 0}
    ctx = _ctx()
    out = {"settled": 0, "pending": 0, "failed": 0}
    for r in rows:
        try:
            st = await _settle_one(ctx, period_id=r.period_id, account_id=r.account_id,
                                   address=r.address, den=float(r.den or 0),
                                   aipg=float(r.aipg_amount), stored_nonce=r.nonce, attempt=1)
            out["settled" if st == "sent" else st] += 1
        except Exception as e:
            out["failed"] += 1
            logger.error("reconcile %s/%s: %s", r.period_id, r.account_id, e)
    return out


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
    ap.add_argument("--retry-failed", action="store_true", help="reconcile pending + retry failed payouts (nonce-bound, idempotent)")
    a = ap.parse_args()
    await init_database()
    try:
        if a.retry_failed:
            print("reconciling pending + failed payouts ...", await reconcile_and_retry()); return
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
