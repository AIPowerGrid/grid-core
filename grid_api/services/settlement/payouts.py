# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Custodial worker payouts (v1) — pay each worker their pro-rata share of a
fixed per-period AIPG budget, by den, via direct ERC-20 transfers on Base.

This is the bootstrap rail BEFORE the trustless on-chain Merkle-claim system
(settlement/bot.py, still a scaffold). It reads the SAME source of truth —
grid_ledger den per worker wallet — so nothing here is throwaway when the
on-chain path lands.

Trust model: the grid (a centralized coordinator today) computes and sends the
payments. Workers trust the grid to pay correctly; the on-chain rail removes that
trust later. Every payout is recorded in grid_payouts with UNIQUE(period_id,
address) so a re-run is idempotent and never double-pays.

DRY-RUN by default: `preview_period` / the CLI compute and print the table and
touch nothing. Sending requires --send + a funded treasury key (env).
"""

import argparse
import asyncio
import datetime as _dt
import logging
import os

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ...database import close_database, init_database, new_session
from ...v2.schema import payouts as payouts_t
from .aggregate import aggregate_den_for_period, count_unattributed_den

logger = logging.getLogger("grid_api.payouts")

# AIPG ERC-20 on Base (the token the website + contracts reference).
AIPG_TOKEN_ADDRESS = os.getenv("AIPG_TOKEN_ADDRESS", "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608")
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "")
TREASURY_PK = os.getenv("SETTLEMENT_TREASURY_PK", "")  # funded AIPG sender; never logged
# Drop dust so a tiny earner's payout doesn't cost more in gas than it's worth.
MIN_AIPG = float(os.getenv("PAYOUT_MIN_AIPG", "0.01"))

_ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]


# ── pure math (no I/O) ───────────────────────────────────────────────────────

def compute_payouts(rows: list[dict], budget_aipg: float, *, min_aipg: float = MIN_AIPG) -> list[dict]:
    """Split `budget_aipg` across workers pro-rata by den. Emissions-funded
    bootstrap → the whole budget goes to the supply side (the 85/3/12 revenue
    split is a REVENUE concept and doesn't apply to emissions). Returns
    [{address, den, share, aipg}] sorted high→low, dropping sub-dust rows."""
    total_den = sum(float(r["den"]) for r in rows)
    if total_den <= 0 or budget_aipg <= 0:
        return []
    out = []
    for r in rows:
        share = float(r["den"]) / total_den
        aipg = budget_aipg * share
        if aipg >= min_aipg:
            out.append({"address": r["address"], "den": float(r["den"]),
                        "share": share, "aipg": round(aipg, 8)})
    return sorted(out, key=lambda x: x["aipg"], reverse=True)


# ── period selection ─────────────────────────────────────────────────────────

def _window(days: float | None, since: str | None, until: str | None) -> tuple[_dt.datetime, _dt.datetime, str]:
    now = _dt.datetime.now(_dt.timezone.utc)
    end = _dt.datetime.fromisoformat(until) if until else now
    if since:
        start = _dt.datetime.fromisoformat(since)
    else:
        start = end - _dt.timedelta(days=days if days is not None else 1.0)
    period_id = f"{start.date().isoformat()}_{end.date().isoformat()}"
    return start, end, period_id


# ── dry-run preview ──────────────────────────────────────────────────────────

async def preview_period(start, end, budget_aipg: float) -> dict:
    rows = await aggregate_den_for_period(start, end)
    payouts = compute_payouts(rows, budget_aipg)
    unattr = await count_unattributed_den(start, end)
    return {
        "workers": len(rows),
        "payable": len(payouts),
        "total_den": sum(float(r["den"]) for r in rows),
        "budget_aipg": budget_aipg,
        "payouts": payouts,
        "unattributed": unattr,
    }


# ── send (gated, idempotent) ─────────────────────────────────────────────────

async def _already_paid(period_id: str, address: str) -> bool:
    async with await new_session() as s:
        row = (await s.execute(
            sa.select(payouts_t.c.status).where(
                payouts_t.c.period_id == period_id, payouts_t.c.address == address)
        )).first()
        return bool(row) and row[0] in ("sent", "confirmed")


async def _record(period_id, address, den, aipg, status, tx_hash=None) -> bool:
    """Insert/advance a payout row. Returns False if (period,address) already
    claimed by a sent/confirmed row (idempotency stop)."""
    async with await new_session() as s:
        try:
            await s.execute(sa.insert(payouts_t).values(
                period_id=period_id, address=address, den=den,
                aipg_amount=aipg, status=status, tx_hash=tx_hash))
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            # row exists → only advance pending→sent/confirmed, never re-pay
            await s.execute(sa.update(payouts_t)
                            .where(payouts_t.c.period_id == period_id,
                                   payouts_t.c.address == address,
                                   payouts_t.c.status == "pending")
                            .values(status=status, tx_hash=tx_hash))
            await s.commit()
            return False


async def send_period(start, end, budget_aipg: float, period_id: str) -> dict:
    """Execute AIPG transfers for a period. Idempotent per (period, address)."""
    if not (BASE_RPC_URL and TREASURY_PK):
        raise RuntimeError("SETTLEMENT_TREASURY_PK + BASE_RPC_URL required to send")
    from web3 import Web3  # imported lazily so dry-run needs no web3
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    acct = w3.eth.account.from_key(TREASURY_PK)
    token = w3.eth.contract(address=Web3.to_checksum_address(AIPG_TOKEN_ADDRESS), abi=_ERC20_ABI)
    decimals = token.functions.decimals().call()

    rows = await aggregate_den_for_period(start, end)
    payouts = compute_payouts(rows, budget_aipg)
    sent = skipped = failed = 0
    nonce = w3.eth.get_transaction_count(acct.address)
    for p in payouts:
        addr = Web3.to_checksum_address(p["address"])
        if await _already_paid(period_id, addr):
            skipped += 1
            continue
        amount_wei = int(round(p["aipg"] * (10 ** decimals)))
        await _record(period_id, addr, p["den"], p["aipg"], "pending")
        try:
            tx = token.functions.transfer(addr, amount_wei).build_transaction({
                "from": acct.address, "nonce": nonce,
                "maxFeePerGas": w3.to_wei(0.1, "gwei"),
                "maxPriorityFeePerGas": w3.to_wei(0.01, "gwei"),
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            await _record(period_id, addr, p["den"], p["aipg"], "sent", tx_hash)
            nonce += 1
            sent += 1
        except Exception as e:
            await _record(period_id, addr, p["den"], p["aipg"], "failed", str(e)[:60])
            logger.error("payout failed addr=%s: %s", addr, e)
            failed += 1
    return {"sent": sent, "skipped": skipped, "failed": failed, "period_id": period_id}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_preview(pv: dict, period_id: str) -> None:
    print(f"\n=== payout preview — period {period_id} ===")
    print(f"workers={pv['workers']}  payable={pv['payable']}  total_den={pv['total_den']:.2f}  "
          f"budget={pv['budget_aipg']:.2f} AIPG")
    u = pv.get("unattributed") or {}
    if u:
        print(f"unattributed (no wallet → unpayable): {u}")
    print(f"{'address':44}{'den':>14}{'share':>9}{'AIPG':>16}")
    for p in pv["payouts"]:
        print(f"{p['address']:44}{p['den']:>14.2f}{p['share']*100:>8.2f}%{p['aipg']:>16.4f}")
    print(f"{'TOTAL':44}{'':>14}{'':>9}{sum(p['aipg'] for p in pv['payouts']):>16.4f}\n")


async def _amain():
    ap = argparse.ArgumentParser(description="Custodial AIPG worker payouts (dry-run by default).")
    ap.add_argument("--days", type=float, default=1.0, help="lookback window (default 1 day)")
    ap.add_argument("--since", help="ISO start (overrides --days)")
    ap.add_argument("--until", help="ISO end (default now)")
    ap.add_argument("--budget", type=float, required=True, help="total AIPG to distribute this period")
    ap.add_argument("--period-id", help="override period id (default <start>_<end>)")
    ap.add_argument("--send", action="store_true", help="EXECUTE transfers (default: dry-run)")
    a = ap.parse_args()
    start, end, pid = _window(a.days, a.since, a.until)
    if a.period_id:
        pid = a.period_id
    await init_database()   # standalone CLI: wire the DB engine the app normally inits
    try:
        pv = await preview_period(start, end, a.budget)
        _print_preview(pv, pid)
        if a.send:
            if not pv["payouts"]:
                print("nothing to send."); return
            print(f"SENDING {len(pv['payouts'])} payouts for {pid} ...")
            print(await send_period(start, end, a.budget, pid))
        else:
            print("(dry-run — re-run with --send to execute)")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(_amain())
