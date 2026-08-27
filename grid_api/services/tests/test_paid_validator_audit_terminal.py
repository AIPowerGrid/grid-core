# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Atomic ordinary-worker payout plus compensated-audit budget proofs."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, ledger
from grid_api.services import validator_audit_budgets as budgets
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import ledger as ledger_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import reservations as demand_reservations_t
from grid_api.v2.schema import validator_audit_budget_counters as counters_t
from grid_api.v2.schema import validator_audit_jobs as audits_t
from grid_api.v2.schema import validators as validators_t
from grid_api.v2.schema import workers as workers_t

NOW = datetime(2026, 8, 27, 16, 0, tzinfo=UTC)
MODEL = "gpt-oss-120b"
WALLET = "0x" + "5" * 40
LIMIT = 100_000_000


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old = database._session_factory
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    database._session_factory = factory
    try:
        yield factory
    finally:
        database._session_factory = old
        await engine.dispose()


async def _seed(db, *, modality="text", model=MODEL):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    second_worker_id = uuid.uuid4()
    validator_id = f"val_terminal_{uuid.uuid4().hex}"
    async with db() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet=WALLET,
                software_version="terminal-test",
                capabilities=[f"{modality}.basic.v1"],
                registration_signature="0x" + "6" * 130,
                status="active",
                last_heartbeat=NOW,
                operator_group_id="opg_terminal_test_01",
                independence_status="verified",
                qualification_started_at=NOW - timedelta(days=4),
                heartbeat_sample_count=900,
                last_heartbeat_sampled_at=NOW,
                independence_reviewed_at=NOW - timedelta(days=1),
                independence_expires_at=NOW + timedelta(days=29),
                independence_review_ref="review:terminal-test",
                created=NOW - timedelta(days=4),
                updated=NOW,
            ),
        )
        for index, candidate_id in enumerate((worker_id, second_worker_id)):
            await session.execute(
                sa.insert(workers_t).values(
                    id=candidate_id,
                    account_id=account_id,
                    name=f"terminal-{modality}-rig-{index}",
                    type=modality,
                    wallet=WALLET,
                    models=[model],
                    capabilities={},
                    maintenance=False,
                    first_seen=NOW - timedelta(days=10),
                    last_seen=NOW,
                    jobs_completed=100,
                    den_earned=0,
                ),
            )
        await session.commit()
    return validator_id, worker_id, second_worker_id


async def _reserve(
    validator_id,
    worker_id,
    *,
    ttl_seconds=3600,
    modality="text",
    model=MODEL,
):
    job_id = uuid.uuid4()
    request_hash = hashlib.sha256(f"request:{job_id}".encode()).hexdigest()
    await budgets.reserve_audit(
        audit_id=f"aud_{uuid.uuid4().hex}",
        job_id=job_id,
        validator_id=validator_id,
        target_worker_id=worker_id,
        target_worker_name=f"terminal-{modality}-rig-0",
        model=model,
        modality=modality,
        policy_id="policy.blind-text.v1",
        corpus_id="corpus.private.v1",
        request_hash=request_hash,
        reserved_units=5_000_000,
        limits=budgets.AuditBudgetLimits(LIMIT, LIMIT, LIMIT, LIMIT),
        allowed_signing_wallets={WALLET},
        ttl_seconds=ttl_seconds,
        now=NOW,
    )
    return job_id, request_hash


def _ledger_values(
    job_id,
    worker_id,
    request_hash,
    *,
    den=1.25,
    model=MODEL,
    job_type="text",
):
    return {
        "job_id": job_id,
        "worker_id": worker_id,
        "wallet": WALLET,
        "model": model,
        "job_type": job_type,
        "den": den,
        "output_units": 32,
        "duration": 1.0,
        "ttft": 0.1,
        "prompt_hash": request_hash,
        "result_hash": hashlib.sha256(f"result:{job_id}".encode()).hexdigest(),
    }


async def _state(db, job_id):
    async with db() as session:
        audit = (await session.execute(sa.select(audits_t).where(audits_t.c.job_id == job_id))).mappings().one()
        payout_count = int(
            await session.scalar(
                sa.select(sa.func.count()).select_from(ledger_t).where(ledger_t.c.job_id == job_id),
            )
            or 0,
        )
        counters = (await session.execute(sa.select(counters_t))).mappings().all()
    return audit, payout_count, counters


@pytest.mark.asyncio
async def test_ordinary_terminal_atomically_pays_worker_and_consumes_audit_budget(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, request_hash = await _reserve(validator_id, worker_id)
    values = _ledger_values(job_id, worker_id, request_hash)

    assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "audit_settled"
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "settled"
    assert int(audit["actual_units"]) == 1_250_000
    assert payout_count == 1
    assert len(counters) == 4
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 1_250_000 for row in counters)

    assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "duplicate"
    _, payout_count, counters = await _state(db, job_id)
    assert payout_count == 1
    assert all(int(row["spent_units"]) == 1_250_000 for row in counters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("modality", "model"),
    [("image", "Krea 2 Turbo"), ("video", "LTX-Video")],
)
async def test_media_terminal_uses_same_atomic_audit_settlement(db, modality, model):
    validator_id, worker_id, _ = await _seed(db, modality=modality, model=model)
    job_id, request_hash = await _reserve(
        validator_id,
        worker_id,
        modality=modality,
        model=model,
    )

    assert (
        await credits.record_and_settle(
            ledger_values=_ledger_values(
                job_id,
                worker_id,
                request_hash,
                model=model,
                job_type=modality,
            ),
            exact=True,
        )
        == "audit_settled"
    )

    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "settled"
    assert audit["modality"] == modality
    assert payout_count == 1
    assert all(int(row["reserved_units"]) == 0 for row in counters)
    assert all(int(row["spent_units"]) == 1_250_000 for row in counters)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["worker", "model", "request", "over_budget"])
async def test_terminal_mismatch_rolls_back_payout_and_budget(db, mismatch):
    validator_id, worker_id, second_worker_id = await _seed(db)
    job_id, request_hash = await _reserve(validator_id, worker_id)
    values = _ledger_values(job_id, worker_id, request_hash)
    if mismatch == "worker":
        values["worker_id"] = second_worker_id
    elif mismatch == "model":
        values["model"] = "wrong-model"
    elif mismatch == "request":
        values["prompt_hash"] = hashlib.sha256(b"wrong-request").hexdigest()
    else:
        values["den"] = 6.0

    assert await credits.record_and_settle(ledger_values=values, completion_tokens=32) == "error"
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "held"
    assert payout_count == 0
    assert all(int(row["reserved_units"]) == 5_000_000 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)


@pytest.mark.asyncio
async def test_failure_release_and_late_success_never_mint_payout(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, request_hash = await _reserve(validator_id, worker_id)
    await credits.release_job(job_id)
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "released"
    assert payout_count == 0
    assert all(int(row["reserved_units"]) == 0 for row in counters)

    assert (
        await credits.record_and_settle(
            ledger_values=_ledger_values(job_id, worker_id, request_hash),
            completion_tokens=32,
        )
        == "stale_no_payout"
    )
    _, payout_count, counters = await _state(db, job_id)
    assert payout_count == 0
    assert all(int(row["spent_units"]) == 0 for row in counters)


@pytest.mark.asyncio
async def test_expired_hold_without_ledger_releases_all_scopes(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, _ = await _reserve(validator_id, worker_id, ttl_seconds=60)
    assert await budgets.sweep_expired_audits(now=NOW + timedelta(seconds=61)) == {
        "released": 1,
        "manual_review": 0,
    }
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "released"
    assert payout_count == 0
    assert all(int(row["reserved_units"]) == 0 for row in counters)


@pytest.mark.asyncio
async def test_expired_hold_with_orphan_ledger_fails_closed_to_manual_review(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, request_hash = await _reserve(validator_id, worker_id, ttl_seconds=60)
    async with db() as session:
        await ledger.record_completion_in_session(
            session,
            **_ledger_values(job_id, worker_id, request_hash),
        )
        await session.commit()

    assert await budgets.sweep_expired_audits(now=NOW + timedelta(seconds=61)) == {
        "released": 0,
        "manual_review": 1,
    }
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "manual_review"
    assert audit["failure_code"] == "expired_with_completion_ledger"
    assert payout_count == 1
    assert all(int(row["reserved_units"]) == 5_000_000 for row in counters)
    assert all(int(row["spent_units"]) == 0 for row in counters)


@pytest.mark.asyncio
async def test_existing_orphan_ledger_is_quarantined_not_double_paid_or_refunded(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, request_hash = await _reserve(validator_id, worker_id)
    values = _ledger_values(job_id, worker_id, request_hash)
    async with db() as session:
        await ledger.record_completion_in_session(session, **values)
        await session.commit()

    assert await credits.record_and_settle(ledger_values=values) == "audit_manual_review"
    audit, payout_count, counters = await _state(db, job_id)
    assert audit["status"] == "manual_review"
    assert audit["failure_code"] == "ledger_without_audit_settlement"
    assert payout_count == 1
    assert all(int(row["reserved_units"]) == 5_000_000 for row in counters)


@pytest.mark.asyncio
async def test_demand_reservation_cannot_be_added_after_audit_hold(db):
    validator_id, worker_id, _ = await _seed(db)
    job_id, _ = await _reserve(validator_id, worker_id)
    async with db() as session:
        with pytest.raises(budgets.AuditBudgetError, match="compensated-audit"):
            await credits._insert_reservation_in_session(
                session,
                job_id,
                uuid.uuid4(),
                MODEL,
                100,
                10,
            )
        await session.rollback()
    async with db() as session:
        assert not await session.scalar(
            sa.select(sa.exists().where(demand_reservations_t.c.job_id == str(job_id))),
        )


def test_den_conversion_is_integer_bounded_and_rounds_against_budget():
    assert budgets.den_to_units(1) == 1_000_000
    assert budgets.den_to_units("0.0000001") == 1
    assert budgets.den_to_units(Decimal("1.0000001")) == 1_000_001
    for invalid in (0, -1, "nan", "inf"):
        with pytest.raises(budgets.AuditBudgetError):
            budgets.den_to_units(invalid)


@pytest.mark.asyncio
async def test_opaque_legacy_duplicate_cannot_be_misclassified_as_audit_error():
    assert await budgets.reconcile_duplicate_terminal(job_id="legacy-demand-job") == "duplicate"
