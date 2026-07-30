# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits
from grid_api.services.canary_audit import JobExpectation, audit_demand_canary
from grid_api.v2.schema import accounts, metadata
from grid_api.v2.schema import credits as credits_t

IMAGE_MODEL = "z-image-turbo"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    previous = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = previous
        await engine.dispose()


def _ledger_values(job_id):
    return {
        "job_id": job_id,
        "worker_id": uuid.uuid4(),
        "wallet": "",
        "model": IMAGE_MODEL,
        "job_type": "image",
        "den": 1.0,
        "output_units": 1,
        "prompt_hash": "prompt-hash",
        "result_hash": "result-hash",
        "duration": 1.0,
    }


@pytest.mark.asyncio
async def test_canary_audit_reconciles_success_failure_and_pre_dispatch_rejection(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    account_id = uuid.uuid4()
    success_job = uuid.uuid4()
    failure_job = uuid.uuid4()
    absent_job = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts).values(id=account_id, flags={}))
        await session.commit()
    await credits.credit(account_id, 1_000_000, "test_topup", ref="funding-receipt")

    await credits.authorize_media(
        account_id,
        IMAGE_MODEL,
        "image",
        1,
        None,
        success_job,
        record_reservation=True,
    )
    assert await credits.record_and_settle(
        ledger_values=_ledger_values(success_job),
        exact=True,
    ) == "settled"

    await credits.authorize_media(
        account_id,
        IMAGE_MODEL,
        "image",
        1,
        None,
        failure_job,
        record_reservation=True,
    )
    await credits.release_job(failure_job)

    async with await database.new_session() as session:
        report = await audit_demand_canary(
            session,
            account_id,
            [
                JobExpectation(success_job, "success"),
                JobExpectation(failure_job, "failure"),
                JobExpectation(absent_job, "absent"),
            ],
            stale_seconds=3600,
        )

    assert report["ok"] is True
    assert report["findings"] == []
    assert [job["expected"] for job in report["jobs"]] == ["success", "failure", "absent"]


@pytest.mark.asyncio
async def test_canary_audit_fails_on_balance_cache_drift(db):
    account_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts).values(id=account_id, flags={}))
        await session.execute(sa.insert(credits_t).values(account_id=account_id, balance_micro=1))
        await session.commit()

    async with await database.new_session() as session:
        report = await audit_demand_canary(session, account_id, [], stale_seconds=3600)

    assert report["ok"] is False
    assert {finding["code"] for finding in report["findings"]} == {
        "account_ledger_drift",
        "global_ledger_drift",
    }
