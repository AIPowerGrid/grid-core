# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Externally funded credits remain a conserved subset of spendable value."""

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, identities
from grid_api.v2.schema import accounts, credit_ledger, credits as balances, metadata


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    schema = "funding_test_" + uuid.uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("CREDITS_TEST_DB_URL", "")
        if not url.startswith("postgresql+asyncpg://"):
            pytest.skip("CREDITS_TEST_DB_URL required for PostgreSQL funding proof")
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
        monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
        yield request.param
    finally:
        if request.param == "postgres":
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(schema, cascade=True, if_exists=True))
        await engine.dispose()


async def account(funded=50, grant=50):
    aid = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(accounts.insert().values(id=aid))
        if funded:
            # Emulates the verified deposit adapter, never a public reason label.
            await credits._credit_in_session(session, aid, funded, "usdc_deposit", f"deposit:{aid}", funded_micro=funded)
        await session.commit()
    if grant:
        assert await credits.credit(aid, grant, "grant:manual", ref=f"grant:{aid}")
    return aid


async def balance(aid):
    async with await database.new_session() as session:
        row = (await session.execute(sa.select(balances).where(balances.c.account_id == aid))).mappings().one()
        totals = (await session.execute(sa.select(
            sa.func.sum(credit_ledger.c.delta_micro), sa.func.sum(credit_ledger.c.funded_delta_micro),
        ).where(credit_ledger.c.account_id == aid))).one()
    assert (row["balance_micro"], row["funded_balance_micro"]) == tuple(totals)
    return tuple(totals)


@pytest.mark.asyncio
async def test_grant_cannot_claim_deposit_provenance_by_reason(db):
    aid = await account(0, 100)
    assert await credits.credit(aid, 200, "usdc_deposit", ref="not-a-receipt")
    assert await balance(aid) == (300, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [0, 20, 50, 80, 100])
async def test_reserve_refund_conserves_funded_subset(db, actual):
    aid = await account()
    assert await credits.debit(aid, 100, "reserve:chat", ref="job") == "ok"
    assert await balance(aid) == (0, 0)
    if actual < 100:
        assert await credits.credit(aid, 100 - actual, "reconcile:refund", ref="job:refund")
        assert not await credits.credit(aid, 100 - actual, "reconcile:refund", ref="job:refund")
    assert await balance(aid) == (100 - actual, max(0, 50 - actual))


@pytest.mark.asyncio
async def test_release_restores_funding_through_durable_terminal(db, monkeypatch):
    aid = await account()
    job = str(uuid.uuid4())
    auth = await credits.authorize_request({"account_id": aid}, "gpt-oss-120b", 10, 10, job, record_reservation=True)
    assert auth["ok"] and 0 < auth["reserved"] < 50
    assert await balance(aid) == (100 - auth["reserved"], 50 - auth["reserved"])
    await credits.release_job(job)
    await credits.release_job(job)
    assert await balance(aid) == (100, 50)


@pytest.mark.asyncio
async def test_settlement_returns_only_unused_funded_credit(db):
    aid = await account(200, 800)
    job = str(uuid.uuid4())
    auth = await credits.authorize_request({"account_id": aid}, "gpt-oss-120b", 1000, 1000, job, record_reservation=True)
    assert auth["ok"] and auth["reserved"] > 200
    await credits.settle_job(job, 100)
    actual = credits.pricing.quote_text("gpt-oss-120b", 1000, 100)
    assert 0 < actual < 200
    assert await balance(aid) == (1000 - actual, 200 - actual)
    await credits.settle_job(job, 100)
    assert await balance(aid) == (1000 - actual, 200 - actual)


@pytest.mark.asyncio
async def test_insufficient_and_duplicate_debits_preserve_lineage(db):
    aid = await account()
    assert await credits.debit(aid, 101, "reserve:chat", ref="too-much") == "insufficient"
    assert await balance(aid) == (100, 50)
    assert await credits.debit(aid, 20, "reserve:chat", ref="job") == "ok"
    assert await credits.debit(aid, 20, "reserve:chat", ref="job") == "already"
    assert await balance(aid) == (80, 30)
    async with await database.new_session() as session:
        assert not await credits._try_extra_debit_in_session(session, aid, 81, "job:extra")
        await session.commit()
    assert await balance(aid) == (80, 30)


@pytest.mark.asyncio
async def test_merge_keeps_grants_and_funded_values_distinct(db):
    source, destination = await account(), await account(10, 200)
    await credits.debit(source, 20, "reserve:chat", ref="old-job")
    await identities.merge_accounts(destination, source, merge_ref="funding-merge")
    assert await balance(source) == (0, 0)
    assert await balance(destination) == (290, 40)
    assert await credits.debit(source, 100, "reserve:chat", ref="after-merge") == "ok"
    assert await balance(destination) == (190, 0)


@pytest.mark.asyncio
async def test_postgres_racers_cannot_overdraw_either_subset(db):
    if db != "postgres":
        pytest.skip("independent concurrent transactions require PostgreSQL")
    aid = await account(3000, 2000)
    results = await asyncio.wait_for(asyncio.gather(*[
        credits.debit(aid, 1000, "reserve:chat", ref=f"race:{i}") for i in range(25)
    ]), timeout=30)
    assert results.count("ok") == 5 and results.count("insufficient") == 20
    assert await balance(aid) == (0, 0)


@pytest.mark.asyncio
async def test_health_detects_funding_drift_without_spendable_drift(db):
    aid = await account()
    assert (await credits.billing_health())["ok"]
    async with await database.new_session() as session:
        await session.execute(balances.update().where(balances.c.account_id == aid).values(funded_balance_micro=49))
        await session.commit()
    report = await credits.billing_health()
    assert not report["ok"]
    assert report["mismatched_funded_accounts"] == 1
    assert report["mismatched_accounts"] == 0


def migrate(connection, direction):
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0042_funded_credit_lineage.py"
    spec = importlib.util.spec_from_file_location("funded_credit_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(revision, direction)()


@pytest.mark.asyncio
async def test_migration_preserves_existing_value_without_inventing_provenance(db):
    aid = await account(0, 12345)
    async with await database.new_session() as session:
        connection = await session.connection()
        await connection.run_sync(migrate, "downgrade")
        await connection.run_sync(migrate, "upgrade")
        await session.commit()
    assert await balance(aid) == (12345, 0)


@pytest.mark.asyncio
async def test_downgrade_refuses_spent_funding_even_when_balance_is_zero(db):
    aid = await account(100, 0)
    assert await credits.debit(aid, 100, "reserve:chat", ref="spent") == "ok"
    async with await database.new_session() as session:
        connection = await session.connection()
        with pytest.raises(RuntimeError, match="refuse to discard"):
            await connection.run_sync(migrate, "downgrade")
    assert await balance(aid) == (0, 0)


@pytest.mark.asyncio
async def test_database_rejects_funded_balance_greater_than_spendable(db):
    aid = await account()
    async with await database.new_session() as session:
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(balances.update().where(balances.c.account_id == aid).values(funded_balance_micro=101))
        await session.rollback()
    assert await balance(aid) == (100, 50)
