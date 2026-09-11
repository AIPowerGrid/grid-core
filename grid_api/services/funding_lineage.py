# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline, preview-first opening reconciliation for migration 0042.

Stop credit writers and drain holds first. Never infer funding from a credit's
reason label or rewrite historical rows. The approved opening adds zero-value
ledger entries carrying provenance only; customer balances are unchanged.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import defaultdict
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .. import database
from ..config import get_settings
from ..v2.schema import credit_ledger, credits, deposits, reservations

_REFUNDS = {"reconcile:refund", "release:failed", "refund:media"}
_OPENING = "funding:opening"
_MAX_ROWS = 100_000


def opening_plan(rows: list[dict], receipts: list[dict], balances: list[dict]) -> dict:
    """Replay a bounded, immutable pre-lineage snapshot, refusing ambiguity."""
    proofs = {}
    for receipt in receipts:
        if receipt["status"] != "credited" or receipt["chain_id"] != 8453 or receipt["asset"] not in {"USDC", "ETH", "AIPG"}:
            raise ValueError("unsupported historical deposit requires review")
        ref = f"base:{receipt['chain_id']}:{receipt['asset'].lower()}:{receipt['tx_hash']}"
        if ref in proofs or receipt["credited_micro"] <= 0:
            raise ValueError("ambiguous historical deposit")
        proofs[ref] = (str(receipt["account_id"]), int(receipt["credited_micro"]))

    spendable, funded = defaultdict(int), defaultdict(int)
    debits, merges, seen = {}, {}, set()
    for row in sorted(rows, key=lambda item: item["id"]):
        aid, ref, reason, delta = str(row["account_id"]), row["ref"], row["reason"], int(row["delta_micro"])
        if not ref or ref in seen or row.get("funded_delta_micro", 0) or reason == _OPENING:
            raise ValueError("opening requires unique untouched pre-lineage history")
        seen.add(ref)
        change = 0
        if ref in proofs:
            if proofs.pop(ref) != (aid, delta) or delta <= 0:
                raise ValueError("deposit and credit movement disagree")
            change = delta
        elif reason == "account:merge_out":
            if not ref.endswith(":out") or delta >= 0 or -delta != spendable[aid]:
                raise ValueError("invalid historical merge debit")
            change = -funded[aid]
            merges[ref[:-4]] = (aid, -delta, -change)
        elif reason == "account:merge_in":
            source = merges.pop(ref[:-3], None) if ref.endswith(":in") else None
            if source is None or source[0] == aid or source[1] != delta:
                raise ValueError("invalid historical merge credit")
            change = source[2]
        elif reason in _REFUNDS:
            original = debits.get(ref[:-7]) if ref.endswith(":refund") else None
            if original is None or original[0] != aid or not 0 < delta <= original[1]:
                raise ValueError("unmatched historical refund requires review")
            change = max(0, original[2] - (original[1] - delta))
        elif delta < 0:
            change = -min(funded[aid], -delta)
            debits[ref] = (aid, -delta, -change)
        # Other positive entries remain grants, including deposit-looking labels.
        spendable[aid] += delta
        funded[aid] += change
        if not 0 <= funded[aid] <= spendable[aid]:
            raise ValueError("historical balances require reconciliation")

    if proofs or merges:
        raise ValueError("unmatched deposit or merge evidence")
    cache = {str(row["account_id"]): int(row["balance_micro"]) for row in balances}
    if any(row.get("funded_balance_micro", 0) for row in balances):
        raise ValueError("opening cannot replace existing funded balances")
    if any(cache.get(aid, 0) != spendable.get(aid, 0) for aid in cache.keys() | spendable.keys()):
        raise ValueError("credit ledger and balance cache disagree")
    allocations = {aid: funded.get(aid, 0) for aid in sorted(cache)}
    evidence = {"version": 1, "ledger": rows, "deposits": receipts, "balances": balances}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {"hash": digest, "allocations": allocations, "accounts": len(allocations),
            "funded_micro": sum(allocations.values()), "balance_micro": sum(cache.values()),
            "deposit_micro": sum(int(row["credited_micro"]) for row in receipts)}


async def _snapshot(session):
    if await session.scalar(sa.select(sa.func.count()).select_from(reservations).where(reservations.c.status == "held")):
        raise ValueError("drain held reservations before opening reconciliation")
    snapshots = []
    for table, key in ((credit_ledger, credit_ledger.c.id), (deposits, deposits.c.id), (credits, credits.c.account_id)):
        rows = (await session.execute(sa.select(table).order_by(key).limit(_MAX_ROWS + 1))).mappings().all()
        if len(rows) > _MAX_ROWS:
            raise ValueError("opening snapshot exceeds reviewed bound")
        snapshots.append([dict(row) for row in rows])
    return snapshots


async def reconcile_opening(expected_hash: str | None = None) -> dict:
    """Preview by default; apply only the exact hash under write-excluding locks."""
    if expected_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("expected opening hash must be SHA-256")
    async with await database.new_session() as session:
        postgres = session.get_bind().dialect.name == "postgresql"
        if postgres:
            if expected_hash is None:
                await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            else:
                await session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
                await session.execute(sa.text(
                    "LOCK TABLE grid_accounts, grid_credits, grid_credit_ledger, grid_deposits, grid_reservations "
                    "IN SHARE ROW EXCLUSIVE MODE"))
        else:
            await session.execute(sa.text("BEGIN IMMEDIATE" if expected_hash else "BEGIN"))
        existing = (await session.execute(sa.select(credit_ledger.c.ref).where(credit_ledger.c.reason == _OPENING))).scalars().all()
        if existing:
            if expected_hash and all(ref.startswith(f"{_OPENING}:{expected_hash}:") for ref in existing):
                return {"status": "already", "hash": expected_hash, "accounts": len(existing)}
            raise ValueError("opening was already recorded; do not replay historical funding")
        plan = opening_plan(*await _snapshot(session))
        if expected_hash is not None:
            if expected_hash != plan["hash"]:
                raise ValueError("opening snapshot changed; preview and review again")
            for aid, amount in plan["allocations"].items():
                account_id = UUID(aid)
                await session.execute(credit_ledger.insert().values(
                    account_id=account_id, delta_micro=0, funded_delta_micro=amount,
                    reason=_OPENING, ref=f"{_OPENING}:{expected_hash}:{aid}",
                ))
                await session.execute(credits.update().where(credits.c.account_id == account_id).values(funded_balance_micro=amount))
            await session.commit()
        return {key: value for key, value in plan.items() if key != "allocations"} | {
            "status": "applied" if expected_hash else "preview",
        }


async def _main(expected_hash):
    # This operator command never invokes create_all or starts runtime loops.
    engine = create_async_engine(get_settings().async_database_url)
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        print(json.dumps(await reconcile_opening(expected_hash), sort_keys=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", metavar="REVIEWED_SHA256", help="Apply the exact preview after draining all credit writers")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.apply))
    except Exception as exc:
        # SQL/connection exceptions can contain credentials and account data.
        print(f"Opening reconciliation failed ({type(exc).__name__}); no partial apply committed.", file=sys.stderr)
        raise SystemExit(1) from None
