# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

from grid_api import database
from grid_api.services import credits, funding_lineage
from grid_api.services.tests.test_funded_credit_lineage import account, balance, db  # noqa: F401
from grid_api.v2.schema import credit_ledger, deposits

TX = "0x" + "ab" * 32
REF = f"base:8453:usdc:{TX}"


def row(id, delta, ref, reason="grant:manual", account_id="a"):
    return dict(id=id, delta_micro=delta, ref=ref, reason=reason, account_id=account_id, funded_delta_micro=0)


def receipt():
    return dict(account_id="a", asset="USDC", chain_id=8453, tx_hash=TX, credited_micro=100, status="credited")


def test_replay_traces_deposit_refund_and_merge_without_promoting_grants():
    rows = [row(1, 100, REF, "usdc_deposit"), row(2, 100, "grant"),
            row(3, -80, "job", "reserve:chat"), row(4, 20, "job:refund", "reconcile:refund"),
            row(5, -140, "merge:out", "account:merge_out"),
            row(6, 140, "merge:in", "account:merge_in", "b"),
            row(7, 200, "fake-deposit", "usdc_deposit", "b")]
    plan = funding_lineage.opening_plan(rows, [receipt()], [
        dict(account_id="a", balance_micro=0), dict(account_id="b", balance_micro=340),
    ])
    assert plan["allocations"] == {"a": 0, "b": 40}
    assert plan["funded_micro"] == 40 and plan["balance_micro"] == 340


@pytest.mark.parametrize("bad", ["missing_receipt", "wrong_account", "wrong_value", "orphan_refund", "orphan_merge", "cache_drift", "already_funded"])
def test_replay_refuses_ambiguous_history(bad):
    rows, receipts, balances = [row(1, 100, REF)], [receipt()], [dict(account_id="a", balance_micro=100)]
    if bad == "missing_receipt":
        rows = []
        balances = []
    elif bad == "wrong_account":
        receipts[0]["account_id"] = "b"
    elif bad == "wrong_value":
        receipts[0]["credited_micro"] = 99
    elif bad == "orphan_refund":
        rows.append(row(2, 10, "unknown:refund", "reconcile:refund"))
    elif bad == "orphan_merge":
        rows.append(row(2, 10, "unknown:in", "account:merge_in"))
    elif bad == "cache_drift":
        balances[0]["balance_micro"] = 101
    else:
        rows[0]["funded_delta_micro"] = 100
    with pytest.raises(ValueError):
        funding_lineage.opening_plan(rows, receipts, balances)


async def legacy_deposit():
    aid = await account(0, 100)
    async with await database.new_session() as session:
        await credits._credit_in_session(session, aid, 100, "usdc_deposit", REF)
        await session.execute(deposits.insert().values(
            account_id=aid, chain_id=8453, asset="USDC", tx_hash=TX, block_number=1,
            from_address="0x" + "11" * 20, treasury_address="0x" + "22" * 20,
            amount_raw=100, amount_decimals=6, price_micro=1_000_000, price_source="test",
            price_timestamp=datetime.now(UTC), credited_micro=100,
            refund_address="0x" + "11" * 20, status="credited",
        ))
        await session.commit()
    return aid


@pytest.mark.asyncio
async def test_opening_is_preview_first_append_only_and_idempotent(db):
    aid = await legacy_deposit()
    assert await credits.debit(aid, 80, "reserve:chat", ref="job") == "ok"
    assert await credits.credit(aid, 20, "reconcile:refund", ref="job:refund")
    async with await database.new_session() as session:
        before = (await session.execute(sa.select(credit_ledger).order_by(credit_ledger.c.id))).mappings().all()
    preview = await funding_lineage.reconcile_opening()
    assert preview["status"] == "preview" and preview["funded_micro"] == 40
    assert await balance(aid) == (140, 0)
    result = await funding_lineage.reconcile_opening(preview["hash"])
    assert result["status"] == "applied"
    assert await balance(aid) == (140, 40)
    # Retries after subsequent funded spending must not reset the funded cache.
    await credits.debit(aid, 20, "reserve:chat", ref="later")
    assert (await funding_lineage.reconcile_opening(preview["hash"]))["status"] == "already"
    assert await balance(aid) == (120, 20)
    async with await database.new_session() as session:
        old = (await session.execute(sa.select(credit_ledger).where(credit_ledger.c.id <= before[-1]["id"]).order_by(credit_ledger.c.id))).mappings().all()
    assert old == before


@pytest.mark.asyncio
async def test_changed_opening_snapshot_never_applies(db):
    aid = await legacy_deposit()
    preview = await funding_lineage.reconcile_opening()
    await credits.credit(aid, 10, "grant:manual", ref="new-grant")
    with pytest.raises(ValueError, match="snapshot changed"):
        await funding_lineage.reconcile_opening(preview["hash"])
    assert await balance(aid) == (210, 0)


@pytest.mark.asyncio
async def test_opening_rejects_inflight_reservations(db):
    aid = await legacy_deposit()
    auth = await credits.authorize_request({"account_id": aid}, "gpt-oss-120b", 10, 10, str(uuid4()), record_reservation=True)
    assert auth["ok"]
    with pytest.raises(ValueError, match="drain held"):
        await funding_lineage.reconcile_opening()


@pytest.mark.asyncio
async def test_postgres_opening_race_applies_once(db):
    if db != "postgres":
        pytest.skip("independent concurrent transactions require PostgreSQL")
    aid = await legacy_deposit()
    preview = await funding_lineage.reconcile_opening()
    results = await asyncio.wait_for(asyncio.gather(*[
        funding_lineage.reconcile_opening(preview["hash"]) for _ in range(2)
    ]), timeout=10)
    assert sorted(result["status"] for result in results) == ["already", "applied"]
    assert await balance(aid) == (200, 100)
