# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real PostgreSQL terminal races in a disposable, per-test schema.

These are worker compensation ledger/budget tests, not validator payout sends.
"""

import asyncio
import os
import uuid
from collections import Counter

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import credits
from grid_api.services import validator_audit_budgets as budgets
from grid_api.services.tests import test_paid_validator_audit_terminal as base
from grid_api.v2.schema import metadata

PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG.startswith("postgresql"),
        reason="set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database",
    ),
]


@pytest_asyncio.fixture
async def db(monkeypatch):
    namespace = "validator_terminal_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        PG,
        execution_options={"schema_translate_map": {None: namespace}},
        pool_size=5,
        max_overflow=20,
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
            await connection.run_sync(metadata.create_all)
        created = True
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        yield factory
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        finally:
            await engine.dispose()


async def blocked_race(db, job_id, operations):
    """Observe real blocked backend transactions before releasing our job lock."""
    tasks = []
    try:
        async with db() as owner:
            await budgets.lock_job_in_session(owner, job_id)
            owner_pid = await owner.scalar(sa.text("SELECT pg_backend_pid()"))
            tasks = [asyncio.create_task(operation()) for operation in operations]
            async with asyncio.timeout(15):
                async with db() as observer:
                    while True:
                        waiting = await observer.scalar(
                            sa.text(
                                "SELECT count(*) FROM pg_stat_activity " "WHERE :owner = ANY(pg_blocking_pids(pid))",
                            ),
                            {"owner": owner_pid},
                        )
                        if waiting >= 2:
                            break
                        assert not any(task.done() for task in tasks)
                        await observer.rollback()
                        await asyncio.sleep(0.01)
            await owner.rollback()
        async with asyncio.timeout(30):
            return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("modality", "model"),
    [("text", base.MODEL), ("image", "Krea 2 Turbo"), ("video", "LTX-Video")],
)
async def test_twenty_terminal_racers_record_one_worker_payment(db, modality, model):
    validator, worker, _ = await base._seed(db, modality=modality, model=model)
    job, request_hash = await base._reserve(validator, worker, modality=modality, model=model)
    values = base._ledger_values(job, worker, request_hash, model=model, job_type=modality)

    async def terminal():
        return await credits.record_and_settle(
            ledger_values=values,
            completion_tokens=32,
            exact=modality != "text",
        )

    results = await blocked_race(db, job, [terminal] * 20)
    assert Counter(results) == {"audit_settled": 1, "duplicate": 19}
    audit, payout_count, counters = await base._state(db, job)
    assert audit["status"] == "settled"
    assert int(audit["actual_units"]) == 1_250_000
    assert payout_count == 1 and len(counters) == 4
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 1_250_000 for row in counters)


async def test_release_racing_success_cannot_refund_a_paid_job(db):
    validator, worker, _ = await base._seed(db)
    job, request_hash = await base._reserve(validator, worker)
    values = base._ledger_values(job, worker, request_hash)

    async def terminal():
        return await credits.record_and_settle(ledger_values=values, completion_tokens=32)

    async def release():
        await credits.release_job(job)

    await blocked_race(db, job, [terminal, release])
    audit, payout_count, counters = await base._state(db, job)
    assert audit["status"] in {"settled", "released"}
    settled = audit["status"] == "settled"
    assert payout_count == int(settled) and len(counters) == 4
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == (1_250_000 if settled else 0) for row in counters)
    assert await terminal() == ("duplicate" if settled else "stale_no_payout")
    assert await base._state(db, job) == (audit, payout_count, counters)


@pytest.mark.parametrize("after_budget_write", [False, True])
async def test_backend_termination_rolls_back_both_halves_then_retry(db, monkeypatch, after_budget_write):
    validator, worker, _ = await base._seed(db)
    job, request_hash = await base._reserve(validator, worker)
    values = base._ledger_values(job, worker, request_hash)
    original = budgets.settle_audit_in_session
    terminated = []

    async def terminate_transaction(session, **kwargs):
        result = await original(session, **kwargs) if after_budget_write else None
        pid = await session.scalar(sa.text("SELECT pg_backend_pid()"))
        async with db() as control:
            assert await control.scalar(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        terminated.append(pid)
        if after_budget_write:
            return result
        return await original(session, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(budgets, "settle_audit_in_session", terminate_transaction)
        assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "error"
    assert len(terminated) == 1
    audit, payout_count, counters = await base._state(db, job)
    assert audit["status"] == "held"
    assert payout_count == 0 and len(counters) == 4
    assert all(int(row["reserved_units"]) == 5_000_000 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)
    assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "audit_settled"
    assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "duplicate"
    audit, payout_count, counters = await base._state(db, job)
    assert audit["status"] == "settled" and payout_count == 1
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 1_250_000 for row in counters)
