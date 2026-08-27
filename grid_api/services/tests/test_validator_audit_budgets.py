# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Money-invariant tests for the dark compensated-audit budget foundation."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api.services import validator_audit_budgets as budgets
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import reservations as demand_reservations_t
from grid_api.v2.schema import validator_audit_budget_counters as counters_t
from grid_api.v2.schema import validator_audit_jobs as audits_t
from grid_api.v2.schema import validators as validators_t
from grid_api.v2.schema import workers as workers_t

NOW = datetime(2026, 8, 27, 12, 15, tzinfo=UTC)
MODEL = "gpt-oss-120b"
WALLET = "0x" + "1" * 40
LIMITS = budgets.AuditBudgetLimits(
    global_hourly=1_000,
    worker_hourly=500,
    validator_hourly=400,
    pair_hourly=300,
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def open_session():
        return factory()

    monkeypatch.setattr(budgets, "new_session", open_session)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(db):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    validator_id = f"val_test_{uuid.uuid4().hex}"
    async with db() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet=WALLET,
                software_version="test",
                capabilities=["text.basic.v1"],
                registration_signature="0x" + "2" * 130,
                status="active",
                last_heartbeat=NOW,
                operator_group_id="opg_audit_test_01",
                independence_status="verified",
                qualification_started_at=NOW - timedelta(days=4),
                heartbeat_sample_count=900,
                last_heartbeat_sampled_at=NOW,
                independence_reviewed_at=NOW - timedelta(days=1),
                independence_expires_at=NOW + timedelta(days=29),
                independence_review_ref="review:audit-test",
                created=NOW - timedelta(days=4),
                updated=NOW,
            ),
        )
        await session.execute(
            sa.insert(workers_t).values(
                id=worker_id,
                account_id=account_id,
                name="audit-text-rig",
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


def _contract(validator_id, worker_id, *, units=100, job_id=None, limits=LIMITS):
    job_id = job_id or uuid.uuid4()
    return {
        "audit_id": f"aud_{uuid.uuid4().hex}",
        "job_id": job_id,
        "validator_id": validator_id,
        "target_worker_id": worker_id,
        "target_worker_name": "audit-text-rig",
        "model": MODEL,
        "modality": "text",
        "policy_id": "policy.dynamic-text.v1",
        "corpus_id": "corpus.private.2026-08",
        "request_hash": hashlib.sha256(str(job_id).encode()).hexdigest(),
        "reserved_units": units,
        "limits": limits,
        "allowed_signing_wallets": {WALLET},
        "ttl_seconds": 3600,
        "now": NOW,
    }


async def _state(db):
    async with db() as session:
        jobs = (await session.execute(sa.select(audits_t))).mappings().all()
        counters = (
            (
                await session.execute(
                    sa.select(counters_t).order_by(counters_t.c.scope, counters_t.c.scope_key),
                )
            )
            .mappings()
            .all()
        )
    return jobs, counters


@pytest.mark.asyncio
async def test_reserve_settle_and_duplicate_are_exactly_once(db):
    validator_id, worker_id = await _seed(db)
    contract = _contract(validator_id, worker_id)
    result = await budgets.reserve_audit(**contract)
    assert result["status"] == "reserved"
    jobs, counters = await _state(db)
    assert len(jobs) == 1
    assert len(counters) == 4
    assert {row["scope"] for row in counters} == {"global", "worker", "validator", "pair"}
    assert all(int(row["reserved_units"]) == 100 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)

    result_hash = hashlib.sha256(b"result").hexdigest()
    async with db() as session:
        assert await budgets.settle_audit_in_session(
            session,
            job_id=contract["job_id"],
            actual_units=60,
            result_hash=result_hash,
            now=NOW + timedelta(minutes=1),
        ) == "settled"
        await session.commit()
    jobs, counters = await _state(db)
    assert jobs[0]["status"] == "settled"
    assert int(jobs[0]["actual_units"]) == 60
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 60 for row in counters)

    async with db() as session:
        assert await budgets.settle_audit_in_session(
            session,
            job_id=contract["job_id"],
            actual_units=60,
            result_hash=result_hash,
            now=NOW + timedelta(minutes=2),
        ) == "duplicate"
        await session.commit()
    _, counters = await _state(db)
    assert all(int(row["spent_units"]) == 60 for row in counters)


@pytest.mark.asyncio
async def test_release_returns_all_caps_and_late_settle_cannot_pay(db):
    validator_id, worker_id = await _seed(db)
    contract = _contract(validator_id, worker_id)
    await budgets.reserve_audit(**contract)
    assert await budgets.release_audit(
        job_id=contract["job_id"],
        failure_code="worker_failed",
        now=NOW + timedelta(minutes=1),
    ) == "released"
    assert await budgets.release_audit(
        job_id=contract["job_id"],
        failure_code="worker_failed",
        now=NOW + timedelta(minutes=2),
    ) == "duplicate"
    jobs, counters = await _state(db)
    assert jobs[0]["status"] == "released"
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)

    async with db() as session:
        assert await budgets.settle_audit_in_session(
            session,
            job_id=contract["job_id"],
            actual_units=50,
            result_hash=hashlib.sha256(b"late").hexdigest(),
        ) == "stale_no_payout"
        await session.commit()


@pytest.mark.asyncio
async def test_over_reserve_settlement_rolls_back_unchanged(db):
    validator_id, worker_id = await _seed(db)
    contract = _contract(validator_id, worker_id)
    await budgets.reserve_audit(**contract)
    async with db() as session:
        with pytest.raises(budgets.AuditBudgetError, match="exceeds"):
            await budgets.settle_audit_in_session(
                session,
                job_id=contract["job_id"],
                actual_units=101,
                result_hash=hashlib.sha256(b"too much").hexdigest(),
            )
        await session.rollback()
    jobs, counters = await _state(db)
    assert jobs[0]["status"] == "held"
    assert all(int(row["reserved_units"]) == 100 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wallet", "unreviewed", "expired", "stale"])
async def test_ineligible_validator_fails_closed_without_budget_rows(db, failure):
    validator_id, worker_id = await _seed(db)
    if failure == "wallet":
        contract = _contract(validator_id, worker_id)
        contract["allowed_signing_wallets"] = {"0x" + "9" * 40}
    else:
        values = {
            "unreviewed": {"independence_status": "unreviewed"},
            "expired": {"independence_expires_at": NOW - timedelta(seconds=1)},
            "stale": {"last_heartbeat": NOW - timedelta(hours=1)},
        }[failure]
        async with db() as session:
            await session.execute(
                sa.update(validators_t).where(validators_t.c.id == validator_id).values(**values),
            )
            await session.commit()
        contract = _contract(validator_id, worker_id)
    with pytest.raises(budgets.AuditBudgetError, match="allowlisted and independent"):
        await budgets.reserve_audit(**contract)
    jobs, counters = await _state(db)
    assert jobs == []
    assert counters == []


@pytest.mark.asyncio
async def test_demand_reservation_collision_is_rejected(db):
    validator_id, worker_id = await _seed(db)
    contract = _contract(validator_id, worker_id)
    async with db() as session:
        await session.execute(
            sa.insert(demand_reservations_t).values(
                job_id=str(contract["job_id"]),
                account_id=None,
                model=MODEL,
                reserved_micro=0,
                free_micro=0,
                promo_micro=0,
                prompt_toks=0,
                discount_bps=0,
                billing_source="credits",
                status="held",
                created=NOW,
            ),
        )
        await session.commit()
    with pytest.raises(budgets.AuditBudgetError, match="demand-side"):
        await budgets.reserve_audit(**contract)
    jobs, counters = await _state(db)
    assert jobs == []
    assert counters == []


@pytest.mark.asyncio
async def test_maintenance_worker_cannot_consume_audit_budget(db):
    validator_id, worker_id = await _seed(db)
    async with db() as session:
        await session.execute(
            sa.update(workers_t).where(workers_t.c.id == worker_id).values(maintenance=True),
        )
        await session.commit()
    with pytest.raises(budgets.AuditBudgetError, match="maintenance"):
        await budgets.reserve_audit(**_contract(validator_id, worker_id))
    jobs, counters = await _state(db)
    assert jobs == []
    assert counters == []


@pytest.mark.asyncio
async def test_cap_exhaustion_is_all_or_none_and_cap_is_frozen(db):
    validator_id, worker_id = await _seed(db)
    tight = budgets.AuditBudgetLimits(150, 150, 150, 150)
    first = _contract(validator_id, worker_id, units=100, limits=tight)
    await budgets.reserve_audit(**first)

    expanded = budgets.AuditBudgetLimits(1_000, 1_000, 1_000, 1_000)
    second = _contract(validator_id, worker_id, units=100, limits=expanded)
    with pytest.raises(budgets.AuditBudgetExceeded):
        await budgets.reserve_audit(**second)
    jobs, counters = await _state(db)
    assert len(jobs) == 1
    assert len(counters) == 4
    assert all(int(row["cap_units"]) == 150 for row in counters)
    assert all(int(row["reserved_units"]) == 100 for row in counters)


@pytest.mark.asyncio
async def test_same_job_same_contract_is_idempotent_and_mutation_is_rejected(db):
    validator_id, worker_id = await _seed(db)
    contract = _contract(validator_id, worker_id)
    assert (await budgets.reserve_audit(**contract))["status"] == "reserved"
    assert (await budgets.reserve_audit(**contract))["status"] == "existing"
    changed = dict(contract)
    changed["policy_id"] = "policy.changed.v2"
    with pytest.raises(budgets.AuditBudgetError, match="different audit contract"):
        await budgets.reserve_audit(**changed)
    jobs, counters = await _state(db)
    assert len(jobs) == 1
    assert all(int(row["reserved_units"]) == 100 for row in counters)
