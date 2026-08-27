# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres concurrency proofs for compensated validator-audit budgets."""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api.services import validator_audit_budgets as budgets
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_audit_budget_counters as counters_t
from grid_api.v2.schema import validator_audit_jobs as audits_t
from grid_api.v2.schema import validators as validators_t
from grid_api.v2.schema import workers as workers_t

_PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")
NOW = datetime(2026, 8, 27, 13, 0, tzinfo=UTC)
MODEL = "gpt-oss-120b"
WALLET = "0x" + "3" * 40

pytestmark = pytest.mark.skipif(
    not _PG.startswith("postgresql"),
    reason="set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database",
)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    engine = create_async_engine(_PG)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def open_session():
        return factory()

    monkeypatch.setattr(budgets, "new_session", open_session)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def _seed(factory):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    validator_id = f"val_pg_audit_{uuid.uuid4().hex}"
    async with factory() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet=WALLET,
                software_version="pg-test",
                capabilities=["text.basic.v1"],
                registration_signature="0x" + "4" * 130,
                status="active",
                last_heartbeat=NOW,
                operator_group_id="opg_pg_audit_test_01",
                independence_status="verified",
                qualification_started_at=NOW - timedelta(days=4),
                heartbeat_sample_count=900,
                last_heartbeat_sampled_at=NOW,
                independence_reviewed_at=NOW - timedelta(days=1),
                independence_expires_at=NOW + timedelta(days=29),
                independence_review_ref="review:pg-audit-test",
                created=NOW - timedelta(days=4),
                updated=NOW,
            ),
        )
        await session.execute(
            sa.insert(workers_t).values(
                id=worker_id,
                account_id=account_id,
                name="pg-audit-text-rig",
                type="text",
                wallet=WALLET,
                models=[MODEL],
                capabilities={},
                maintenance=False,
                first_seen=NOW - timedelta(days=10),
                last_seen=NOW,
                jobs_completed=100,
                den_earned=0,
            ),
        )
        await session.commit()
    return validator_id, worker_id


def _contract(validator_id, worker_id, *, job_id=None, audit_id=None, cap=50):
    job_id = job_id or uuid.uuid4()
    return {
        "audit_id": audit_id or f"aud_{uuid.uuid4().hex}",
        "job_id": job_id,
        "validator_id": validator_id,
        "target_worker_id": worker_id,
        "target_worker_name": "pg-audit-text-rig",
        "model": MODEL,
        "modality": "text",
        "policy_id": "policy.pg-dynamic.v1",
        "corpus_id": "corpus.pg-private.v1",
        "request_hash": hashlib.sha256(str(job_id).encode()).hexdigest(),
        "reserved_units": 10,
        "limits": budgets.AuditBudgetLimits(cap, cap, cap, cap),
        "allowed_signing_wallets": {WALLET},
        "now": NOW,
    }


@pytest.mark.asyncio
async def test_global_and_scoped_caps_hold_under_25_racers(pg):
    validator_id, worker_id = await _seed(pg)

    async def attempt():
        try:
            await budgets.reserve_audit(**_contract(validator_id, worker_id))
            return "reserved"
        except budgets.AuditBudgetExceeded:
            return "exceeded"

    outcomes = await asyncio.gather(*(attempt() for _ in range(25)))
    assert outcomes.count("reserved") == 5
    assert outcomes.count("exceeded") == 20
    async with pg() as session:
        jobs = int(await session.scalar(sa.select(sa.func.count()).select_from(audits_t)) or 0)
        counters = (await session.execute(sa.select(counters_t))).mappings().all()
    assert jobs == 5
    assert len(counters) == 4
    assert all(int(row["reserved_units"]) == 50 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)


@pytest.mark.asyncio
async def test_same_job_race_reserves_once(pg):
    validator_id, worker_id = await _seed(pg)
    job_id = uuid.uuid4()
    contract = _contract(
        validator_id,
        worker_id,
        job_id=job_id,
        audit_id=f"aud_{uuid.uuid4().hex}",
        cap=500,
    )
    outcomes = await asyncio.gather(*(budgets.reserve_audit(**contract) for _ in range(20)))
    assert sum(result["status"] == "reserved" for result in outcomes) == 1
    assert sum(result["status"] == "existing" for result in outcomes) == 19
    async with pg() as session:
        jobs = int(await session.scalar(sa.select(sa.func.count()).select_from(audits_t)) or 0)
        counters = (await session.execute(sa.select(counters_t))).mappings().all()
    assert jobs == 1
    assert len(counters) == 4
    assert all(int(row["reserved_units"]) == 10 for row in counters)


@pytest.mark.asyncio
async def test_settle_release_race_has_one_terminal_winner(pg):
    validator_id, worker_id = await _seed(pg)
    contract = _contract(validator_id, worker_id, cap=500)
    await budgets.reserve_audit(**contract)

    async def settle():
        async with pg() as session:
            result = await budgets.settle_audit_in_session(
                session,
                job_id=contract["job_id"],
                actual_units=7,
                result_hash=hashlib.sha256(b"race-result").hexdigest(),
                now=NOW + timedelta(minutes=1),
            )
            await session.commit()
            return result

    settled, released = await asyncio.gather(
        settle(),
        budgets.release_audit(
            job_id=contract["job_id"],
            failure_code="race_release",
            now=NOW + timedelta(minutes=1),
        ),
    )
    assert (settled, released) in {
        ("settled", "settled"),
        ("stale_no_payout", "released"),
    }
    async with pg() as session:
        job = (await session.execute(sa.select(audits_t))).mappings().one()
        counters = (await session.execute(sa.select(counters_t))).mappings().all()
    assert job["status"] in {"settled", "released"}
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    if job["status"] == "settled":
        assert all(int(row["spent_units"]) == 7 for row in counters)
    else:
        assert all(int(row["spent_units"]) == 0 for row in counters)
