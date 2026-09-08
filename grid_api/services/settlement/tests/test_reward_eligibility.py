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
from grid_api.v2.schema import accounts, ledger, metadata, reservations, workers, x402_payments

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


async def seed(*, created=CUTOVER, reservation=None, payment=None, wallet=ADDRESS, compact_job_id=False):
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
