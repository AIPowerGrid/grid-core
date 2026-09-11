# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prospective emission eligibility, including real PostgreSQL query semantics."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.config import GridSettings
from grid_api.services.settlement import aggregate
from grid_api.v2.schema import accounts, credit_ledger, ledger, metadata, reservations, workers, x402_payments

CUTOVER = datetime(2026, 9, 9, tzinfo=UTC)
ADDRESS = "0x" + "12" * 20


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    schema = "reward_test_" + uuid.uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("CREDITS_TEST_DB_URL", "")
        if not url.startswith("postgresql+asyncpg://"):
            pytest.skip("CREDITS_TEST_DB_URL required for PostgreSQL reward query proof")
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            if request.param == "postgres":
                await conn.execute(sa.schema.CreateSchema(schema))
            else:
                await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            await conn.run_sync(metadata.create_all)
        monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=CUTOVER))
        yield
    finally:
        if request.param == "postgres":
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(schema, cascade=True, if_exists=True))
        await engine.dispose()


async def seed(*, created=CUTOVER, reservation=None, payment=None, wallet=ADDRESS, compact_job_id=False,
               funded=True):
    aid, wid, jid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts).values(id=aid, payout_wallet=wallet))
        await session.execute(sa.insert(workers).values(id=wid, account_id=aid, name=str(wid), type="text", wallet=wallet))
        await session.execute(sa.insert(ledger).values(
            job_id=jid, worker_id=wid, wallet=wallet, model="smollm2", job_type="text", den=100.0,
            output_units=100, created=created,
        ))
        if reservation is not None:
            values = dict(job_id=jid.hex if compact_job_id else str(jid), account_id=aid, model="smollm2", reserved_micro=100,
                          actual_micro=100, status="settled", prompt_toks=1, created=created)
            values.update(reservation)
            await session.execute(sa.insert(reservations).values(**values))
            if values["account_id"] and values.get("billing_source", "credits") == "credits":
                paid = max(0, (values["actual_micro"] or 0) - values.get("free_micro", 0) - values.get("promo_micro", 0))
                await session.execute(sa.insert(credit_ledger).values(
                    account_id=aid, ref=values["job_id"], reason="reserve:chat",
                    delta_micro=-paid, funded_delta_micro=-paid if funded else 0,
                ))
        if payment is not None:
            await session.execute(sa.insert(x402_payments).values(
                job_id=str(jid), authorization_id=str(jid), payer=ADDRESS, network="eip155:8453",
                asset=ADDRESS, pay_to=ADDRESS, authorized_micro=100, **payment,
            ))
        await session.commit()
    return aid


@pytest.mark.asyncio
@pytest.mark.parametrize(("reservation", "expected"), [
    (None, 0),
    ({"status": "held"}, 0),
    ({"status": "released"}, 0),
    ({"actual_micro": None}, 0),
    ({"actual_micro": 0}, 0),
    ({"account_id": None}, 0),
    ({"billing_source": "unknown"}, 0),
    ({"free_micro": 100}, 0),
    ({"promo_micro": 100}, 0),
    ({"free_micro": 60, "promo_micro": 30}, 10),
    ({"free_micro": 60, "promo_micro": 30, "actual_micro": 50}, 0),
    ({"free_micro": -1}, 0),
    ({"free_micro": 101}, 0),
    ({}, 100),
])
async def test_new_work_contributes_only_purchased_fraction(db, reservation, expected):
    aid = await seed(reservation=reservation)
    rows = await aggregate.aggregate_den_by_account(CUTOVER, CUTOVER + timedelta(hours=1))
    assert rows == ([] if not expected else [{"account_id": str(aid), "den": expected, "payout_address": ADDRESS, "smollm_den": expected}])
    assert await aggregate.total_den_in_window(CUTOVER, CUTOVER + timedelta(hours=1)) == expected
    assert await aggregate.aggregate_den_for_period(CUTOVER, CUTOVER + timedelta(hours=1)) == (
        [] if not expected else [{"address": ADDRESS, "den": expected}]
    )


@pytest.mark.asyncio
async def test_history_unchanged_but_exact_cutover_is_protected(db):
    await seed(created=CUTOVER - timedelta(microseconds=1))
    await seed(created=CUTOVER)
    assert await aggregate.total_den_in_window(CUTOVER - timedelta(hours=1), CUTOVER + timedelta(hours=1)) == 100
    async with await database.new_session() as session:
        assert (await session.execute(sa.select(sa.func.sum(ledger.c.den)))).scalar_one() == 200
    rows = await aggregate.aggregate_den_by_account(CUTOVER - timedelta(hours=1), CUTOVER + timedelta(hours=1))
    assert len(rows) == 1 and "smollm_den" not in rows[0]


@pytest.mark.asyncio
async def test_unattributed_diagnostics_use_same_eligibility(db):
    await seed(wallet="", reservation={"free_micro": 90})
    await seed(wallet="")
    assert await aggregate.count_unattributed_den(CUTOVER, CUTOVER + timedelta(hours=1)) == {"jobs": 1, "den": 10}


@pytest.mark.asyncio
async def test_compact_uuid_reservation_remains_eligible(db):
    await seed(reservation={}, compact_job_id=True)
    assert await aggregate.total_den_in_window(CUTOVER, CUTOVER + timedelta(hours=1)) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(("reservation", "expected"), [
    (None, 0), ({"status": "held"}, 0), ({"status": "released"}, 0),
    ({"actual_micro": 0}, 0), ({"actual_micro": None}, 0),
    ({"actual_micro": 101}, 0), ({"account_id": None}, 0),
    ({"billing_source": "unknown"}, 0), ({"free_micro": -1}, 0),
    ({"free_micro": 101}, 0), ({"free_micro": 100}, 0),
    ({"promo_micro": 100}, 0), ({"free_micro": 60, "promo_micro": 30}, 10),
    ({}, 100),
])
async def test_demand_backing_is_actual_purchased_work(db, reservation, expected):
    aid = await seed(reservation=reservation)
    backing = await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1))
    assert backing.get(str(aid), 0) == expected


@pytest.mark.asyncio
async def test_granted_purchased_pocket_does_not_back_emissions(db):
    aid = await seed(reservation={}, funded=False)
    assert await aggregate.total_den_in_window(CUTOVER, CUTOVER + timedelta(hours=1)) == 100
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {str(aid): 0}


@pytest.mark.asyncio
async def test_missing_or_wrong_account_movements_do_not_back_emissions(db):
    aid = await seed(reservation={})
    other = await seed()
    async with await database.new_session() as session:
        await session.execute(credit_ledger.update().where(credit_ledger.c.account_id == aid).values(account_id=other))
        await session.commit()
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {str(aid): 0}


@pytest.mark.asyncio
async def test_refund_lineage_reduces_reward_backing(db):
    aid = await seed(reservation={"actual_micro": 60})
    async with await database.new_session() as session:
        row = (await session.execute(sa.select(credit_ledger))).mappings().one()
        await session.execute(credit_ledger.update().values(delta_micro=-100, funded_delta_micro=-80))
        await session.execute(credit_ledger.insert().values(
            account_id=aid, ref=row["ref"] + ":refund", reason="reconcile:refund",
            delta_micro=40, funded_delta_micro=20,
        ))
        await session.commit()
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {str(aid): 60}


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_demand_backing_handles_job_uuid_spellings_and_walletless_work(db, compact):
    aid = await seed(reservation={}, compact_job_id=compact, wallet=None)
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {str(aid): 100}


@pytest.mark.asyncio
async def test_ambiguous_duplicate_reservations_never_double_back_rewards(db):
    await seed(reservation={})
    async with await database.new_session() as session:
        row = dict((await session.execute(sa.select(reservations))).mappings().one())
        row["job_id"] = uuid.UUID(row["job_id"]).hex
        await session.execute(reservations.insert().values(**row))
        await session.commit()
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state,amount,expected", [
    ("reported", 100, 0), ("settled", 99, 0), ("settled", 100, 100),
])
async def test_x402_demand_backing_requires_sufficient_settled_payment(db, state, amount, expected):
    aid = await seed(reservation={"billing_source": "x402", "account_id": None},
                     payment={"status": state, "settled_micro": amount})
    result = await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1))
    assert result.get(str(aid), 0) == expected


@pytest.mark.asyncio
async def test_backing_only_counts_completions_in_exact_period(db):
    await seed(created=CUTOVER - timedelta(microseconds=1), reservation={})
    valid = await seed(reservation={})
    await seed(created=CUTOVER + timedelta(hours=1), reservation={})
    assert await aggregate.purchased_work_by_account(CUTOVER, CUTOVER + timedelta(hours=1)) == {str(valid): 100}


@pytest.mark.asyncio
async def test_real_aggregation_flows_through_frozen_demand_sender(db, monkeypatch):
    from grid_api.config import WorkerRewardDemandPolicy
    from grid_api.services.settlement import payouts as P

    end = CUTOVER + timedelta(hours=1)
    pid = CUTOVER.strftime("hour-%Y-%m-%dT%H")
    aid = await seed(reservation={"reserved_micro": 10000, "actual_micro": 10000})
    async with await database.new_session() as session:
        await session.execute(ledger.update().values(model="qwen3-27b"))
        await session.commit()
    monkeypatch.setattr(P, "get_settings", lambda: SimpleNamespace(
        worker_rewards_paid_only_since=CUTOVER,
        worker_reward_demand_policies=[WorkerRewardDemandPolicy(
            since=CUTOVER, until=end, price_micro_per_aipg=1000)]))
    monkeypatch.setattr(P, "_now", lambda: end + timedelta(hours=1))
    monkeypatch.setattr(P, "BASE_RPC_URL", "")
    monkeypatch.setattr(P, "TREASURY_PK", "")
    monkeypatch.setattr(P, "_ctx", lambda: pytest.fail("must not construct a signer"))
    result = await P.send_period(CUTOVER, end, 208.33, pid)
    assert result["failed"] == 1
    row = await P._row(pid, aid)
    assert row["aipg_amount"] == 8.5 and row["nonce"] is None
    async with await database.new_session() as session:
        plan = await session.scalar(sa.select(P.periods_t.c.plan))
        assert plan["allocations"][0]["purchased_micro"] == 10000
        assert (await session.execute(sa.select(reservations.c.actual_micro))).scalar_one() == 10000
        assert (await session.execute(sa.select(ledger.c.den))).scalar_one() == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "verified", "reported", "settled"])
async def test_x402_requires_external_payment_settlement(db, state):
    await seed(reservation={"billing_source": "x402", "account_id": None},
               payment=None if state is None else {"status": state, "settled_micro": 100})
    assert await aggregate.total_den_in_window(CUTOVER, CUTOVER + timedelta(hours=1)) == (100 if state == "settled" else 0)


@pytest.mark.asyncio
async def test_unset_policy_keeps_legacy_preview(db, monkeypatch):
    monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=None))
    await seed()
    assert await aggregate.total_den_in_window(CUTOVER, CUTOVER + timedelta(hours=1)) == 100


def test_policy_boundary_requires_timezone():
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None, worker_rewards_paid_only_since="2026-09-09T00:00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reservation", "unbacked"), [
    (None, 100), ({"status": "held"}, 100),
    ({"status": "released"}, 100), ({"free_micro": 100}, 100),
    ({"promo_micro": 100}, 100),
    ({"free_micro": 60, "promo_micro": 30}, 90), ({}, 0),
])
async def test_reward_monitor_detects_legacy_unbacked_share(db, monkeypatch, reservation, unbacked):
    monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=None))
    await seed(reservation=reservation)
    report = await aggregate.reward_backing_health(CUTOVER, CUTOVER + timedelta(hours=1))
    assert report == {"unbacked_jobs": int(unbacked > 0), "unbacked_den": unbacked}
    async with await database.new_session() as session:
        assert (await session.execute(sa.select(sa.func.sum(ledger.c.den)))).scalar_one() == 100


@pytest.mark.asyncio
async def test_reward_monitor_does_not_flag_excluded_or_fully_funded_work(db):
    await seed()
    await seed(reservation={"promo_micro": 80})
    await seed(reservation={}, compact_job_id=True)
    await seed(reservation={"billing_source": "x402", "account_id": None},
               payment={"status": "reported", "settled_micro": 100})
    await seed(reservation={"billing_source": "x402", "account_id": None},
               payment={"status": "settled", "settled_micro": 100})
    assert await aggregate.reward_backing_health(CUTOVER, CUTOVER + timedelta(hours=1)) == {
        "unbacked_jobs": 0, "unbacked_den": 0,
    }


@pytest.mark.asyncio
async def test_reward_monitor_window_and_historical_boundary(db):
    await seed(created=CUTOVER - timedelta(hours=2))
    await seed(created=CUTOVER - timedelta(microseconds=1))
    await seed(created=CUTOVER)
    await seed(created=CUTOVER + timedelta(hours=1))
    assert await aggregate.reward_backing_health(CUTOVER - timedelta(hours=1), CUTOVER) == {
        "unbacked_jobs": 1, "unbacked_den": 100,
    }
    assert await aggregate.reward_backing_health(CUTOVER, CUTOVER + timedelta(hours=1)) == {
        "unbacked_jobs": 0, "unbacked_den": 0,
    }


@pytest.mark.asyncio
async def test_reward_monitor_includes_walletless_accrual_not_unattributable(db, monkeypatch):
    monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=None))
    await seed(wallet="")
    orphan = await seed(wallet="")
    async with await database.new_session() as session:
        await session.execute(sa.update(workers).where(workers.c.account_id == orphan).values(account_id=None))
        await session.commit()
    assert await aggregate.reward_backing_health(CUTOVER, CUTOVER + timedelta(hours=1)) == {
        "unbacked_jobs": 1, "unbacked_den": 100,
    }


@pytest.mark.asyncio
async def test_reward_monitor_obeys_x402_funding_gate_even_before_cutoff(db, monkeypatch):
    monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=None))
    await seed(reservation={"billing_source": "x402", "account_id": None}, payment={"status": "reported"})
    assert await aggregate.reward_backing_health(CUTOVER, CUTOVER + timedelta(hours=1)) == {
        "unbacked_jobs": 0, "unbacked_den": 0,
    }
