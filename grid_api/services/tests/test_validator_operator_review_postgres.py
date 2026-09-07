# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres proof that active qualification cannot be reset accidentally."""

import asyncio
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api.config import GridSettings
from grid_api.services import validator_operators, validators
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_assignments as assignments_t
from grid_api.v2.schema import validator_attestations as attestations_t
from grid_api.v2.schema import validators as validators_t

PG_URL = os.environ.get("VALIDATORS_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"),
    reason="set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrades", [
    ["v0.1.0-preview.15", "v0.1.0-preview.16"],
    ["v0.1.0-preview.15", "v0.1.0-preview.16", "v0.1.0-preview.17"],
])
async def test_reviewed_version_transition_preserves_history_on_postgres(pg, monkeypatch, upgrades):
    started = datetime(2026, 9, 1, 12, tzinfo=UTC)
    account_id = uuid4()
    validator_id = "val_" + uuid4().hex
    wallet = "0x" + "2" * 40
    settings = GridSettings(_env_file=None, validator_cohort_upgrade_version="v0.1.0-preview.15")
    monkeypatch.setattr(validator_operators, "get_settings", lambda: settings)

    async def new_session():
        return pg()

    monkeypatch.setattr(validators, "new_session", new_session)
    async with pg() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(sa.insert(validators_t).values(
            id=validator_id, account_id=account_id, signing_wallet=wallet,
            software_version="v0.1.0-preview.15", capabilities=["text.generated.v8"],
            registration_signature="fixture", status="active", last_heartbeat=started,
            independence_status="unreviewed", qualification_started_at=started,
            heartbeat_sample_count=10, last_heartbeat_sampled_at=started,
            created=started, updated=started,
        ))
        await session.commit()

    settings = GridSettings(_env_file=None, validator_cohort_upgrade_versions=upgrades)
    expected_samples = 10
    for index, (version, eligible) in enumerate([
        ("v0.1.0-preview.13", True), ("v0.1.0-preview.15", True),
        ("v0.1.0-preview.16", True), ("v0.1.0-preview.17", "v0.1.0-preview.17" in upgrades),
        ("v0.1.0-preview.18", False),
        ("vv0.1.0-preview.16", False), ("0.1.0-preview.16", True),
    ], start=1):
        now = started + timedelta(minutes=index * 6)
        monkeypatch.setattr(validators, "_now", lambda: now)
        result = await validators.heartbeat_validator(
            account_id=account_id, signing_wallet=wallet, software_version=version,
            capabilities=["text.generated.v8"],
        )
        expected_samples += int(eligible)
        assert result["economic_effect"] == "none"
        async with pg() as session:
            row = (await session.execute(sa.select(validators_t).where(validators_t.c.id == validator_id))).mappings().one()
            sql_eligible = await session.scalar(sa.select(validator_operators.cohort_version_filter(sa.literal(version))))
        assert bool(sql_eligible) == validator_operators.cohort_version_status(version)[1] == eligible
        assert row["qualification_started_at"] == started
        assert row["heartbeat_sample_count"] == expected_samples
        assert len(row["heartbeat_window_samples"]) == expected_samples - 10
        assert row["signing_wallet"] == wallet
        assert row["account_id"] == account_id
        assert row["independence_status"] == "unreviewed"
        assert row["operator_group_id"] is None
        assert row["independence_reviewed_at"] is None

    # Rollback restores the previous eligibility policy without rewriting history.
    settings = GridSettings(_env_file=None, validator_cohort_upgrade_version="v0.1.0-preview.15")
    monkeypatch.setattr(validators, "_now", lambda: started + timedelta(hours=1))
    await validators.heartbeat_validator(
        account_id=account_id, signing_wallet=wallet, software_version="v0.1.0-preview.16",
        capabilities=["text.generated.v8"],
    )
    async with pg() as session:
        row = (await session.execute(sa.select(validators_t).where(validators_t.c.id == validator_id))).mappings().one()
    assert row["qualification_started_at"] == started
    assert row["heartbeat_sample_count"] == expected_samples


@pytest_asyncio.fixture
async def pg(monkeypatch):
    namespace = "validator_operator_review_" + uuid4().hex
    engine = create_async_engine(
        PG_URL,
        execution_options={"schema_translate_map": {None: namespace}},
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
            await connection.run_sync(metadata.create_all)
        created = True
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def new_session():
            return factory()

        monkeypatch.setattr(validator_operators, "new_session", new_session)
        monkeypatch.setattr(
            validator_operators,
            "get_settings",
            lambda: SimpleNamespace(
                validator_cohort_baseline_version="v0.1.0-preview.13",
            ),
        )
        yield factory
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_candidate_requires_explicit_restart_on_postgres(pg):
    now = datetime(2026, 9, 1, 19, tzinfo=UTC)
    started = now - timedelta(hours=24)
    account_id = uuid4()
    validator_id = "val_" + uuid4().hex
    async with pg() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet="0x" + "1" * 40,
                software_version="v0.1.0-preview.13",
                capabilities=["text.generated.v8"],
                registration_signature="fixture",
                status="active",
                last_heartbeat=now,
                operator_group_id="opg_postgres_restart_01",
                independence_status="candidate",
                qualification_started_at=started,
                heartbeat_sample_count=100,
                last_heartbeat_sampled_at=now - timedelta(minutes=5),
                created=started,
                updated=now,
            ),
        )
        await session.commit()

    async def protected_state():
        async with pg() as session:
            row = (
                await session.execute(
                    sa.select(validators_t).where(validators_t.c.id == validator_id),
                )
            ).mappings().one()
        return {
            key: row[key]
            for key in (
                "operator_group_id",
                "independence_status",
                "qualification_started_at",
                "heartbeat_sample_count",
                "last_heartbeat_sampled_at",
                "independence_reviewed_at",
                "independence_expires_at",
                "independence_review_ref",
                "updated",
            )
        }

    original = await protected_state()

    blocked = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-accidental",
        now=now,
    )
    assert blocked["eligible_to_apply"] is False
    with pytest.raises(
        validator_operators.OperatorReviewError,
        match="qualification is already active",
    ):
        await validator_operators.review_operator(
            validator_id,
            action="candidate",
            operator_group_id="opg_postgres_restart_01",
            review_ref="review:postgres-accidental",
            expected_digest=blocked["current_digest"],
            apply=True,
            now=now,
        )

    assert await protected_state() == original

    preview = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-deliberate",
        restart_qualification=True,
        now=now,
    )
    applied = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-deliberate",
        restart_qualification=True,
        expected_digest=preview["current_digest"],
        apply=True,
        now=now,
    )
    assert applied["eligible_to_apply"] is True

    async with pg() as session:
        restarted = (
            await session.execute(
                sa.select(validators_t).where(validators_t.c.id == validator_id),
            )
        ).mappings().one()
    assert restarted["qualification_started_at"] == now
    assert restarted["heartbeat_sample_count"] == 0
    assert restarted["last_heartbeat_sampled_at"] is None
    assert restarted["independence_review_ref"] == "review:postgres-deliberate"
    assert restarted["updated"] == now


@pytest.mark.asyncio
async def test_restart_flag_is_candidate_only_on_postgres(pg):
    with pytest.raises(
        validator_operators.OperatorReviewError,
        match="valid only for a candidate transition",
    ):
        await validator_operators.review_operator(
            "val_missing",
            action="verify",
            review_ref="review:postgres-invalid-restart",
            restart_qualification=True,
        )


async def _recovery_candidate(pg, now):
    account_id = uuid4()
    validator_id = "val_" + uuid4().hex
    wallet = "0x" + "3" * 40
    end = int(now.timestamp()) // validator_operators.RECOVERY_BUCKET_SECONDS
    values = dict(
        id=validator_id, account_id=account_id, signing_wallet=wallet,
        software_version="v0.1.0-preview.13", capabilities=["text.generated.v8"],
        registration_signature="fixture", status="active", last_heartbeat=now,
        independence_status="candidate", operator_group_id="opg_recovery_test_01",
        qualification_started_at=now - timedelta(days=20), heartbeat_sample_count=227,
        last_heartbeat_sampled_at=now - timedelta(minutes=6),
        heartbeat_window_started_at=now - timedelta(days=4),
        heartbeat_window_samples=list(range(end - 863, end + 1)),
        created=now - timedelta(days=20), updated=now,
    )
    async with pg() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(sa.insert(validators_t).values(**values))
        await session.commit()
    return values


async def _recovery_evidence(pg, candidate, created):
    assignment_id = "asg_" + uuid4().hex
    async with pg() as session:
        await session.execute(sa.insert(assignments_t).values(
            id=assignment_id, account_id=candidate["account_id"], validator_id=candidate["id"],
            grid_nonce=uuid4().hex, target_worker_id="fixture-worker", target_worker_name="fixture",
            model="fixture", modality="text", capability="text.generated.v8", canary_kind="math.add",
            scoring_policy_id="fixture", probe_status="completed", created=created,
            expires=created + timedelta(hours=1),
        ))
        await session.execute(sa.insert(attestations_t).values(
            attestation_hash=uuid4().hex * 2, account_id=candidate["account_id"],
            validator_id=candidate["id"], assignment_id=assignment_id,
            authority="authoritative", verdict="healthy", signature_status="verified",
            payload={"fixture": True}, created=created,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_recent_recovery_requires_recent_work_and_preserves_history(pg):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    candidate = await _recovery_candidate(pg, now)
    await _recovery_evidence(pg, candidate, now - timedelta(days=4))
    kwargs = dict(action="verify", review_ref="review:recent-recovery", now=now)
    preview = await validator_operators.review_operator(candidate["id"], **kwargs)
    assert preview["qualification"]["coverage_basis"] == "recent_72h"
    assert preview["qualification"]["coverage_ready"]
    assert not preview["eligible_to_apply"]
    assert preview["activity"] == {"assigned": 0, "completed": 0, "attested": 0}
    await _recovery_evidence(pg, candidate, now - timedelta(minutes=10))
    preview = await validator_operators.review_operator(candidate["id"], **kwargs)
    assert preview["eligible_to_apply"]
    await validator_operators.review_operator(candidate["id"], **kwargs,
        apply=True, expected_digest=preview["current_digest"])
    async with pg() as session:
        state = (await session.execute(sa.select(validators_t).where(validators_t.c.id == candidate["id"]))).mappings().one()
        for key in (
            "account_id", "signing_wallet", "qualification_started_at", "heartbeat_sample_count",
            "heartbeat_window_started_at", "heartbeat_window_samples", "operator_group_id",
        ):
            assert state[key] == candidate[key]
        assert state["independence_status"] == "verified"
        for name in (
            "grid_ledger", "grid_credit_ledger", "grid_reservations", "grid_payouts",
            "grid_validator_audit_jobs", "grid_validator_audit_budget_counters",
        ):
            assert await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables[name])) == 0


@pytest.mark.asyncio
async def test_concurrent_recent_heartbeats_count_once_on_postgres(pg, monkeypatch):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    candidate = await _recovery_candidate(pg, now)
    async with pg() as session:
        await session.execute(sa.update(validators_t).where(validators_t.c.id == candidate["id"]).values(
            heartbeat_window_samples=[], heartbeat_window_started_at=None,
        ))
        await session.commit()
    async def new_session():
        return pg()
    monkeypatch.setattr(validators, "new_session", new_session)
    monkeypatch.setattr(validators, "_now", lambda: now)
    results = await asyncio.gather(*[validators.heartbeat_validator(
        account_id=candidate["account_id"], signing_wallet=candidate["signing_wallet"],
        software_version="v0.1.0-preview.13", capabilities=["text.generated.v8"],
    ) for _ in range(25)])
    assert all(result["economic_effect"] == "none" for result in results)
    async with pg() as session:
        state = (await session.execute(sa.select(validators_t).where(validators_t.c.id == candidate["id"]))).mappings().one()
    assert state["heartbeat_window_samples"] == [int(now.timestamp()) // 300]
    assert state["heartbeat_sample_count"] == candidate["heartbeat_sample_count"] + 1
    assert state["qualification_started_at"] == candidate["qualification_started_at"]
    assert state["heartbeat_window_started_at"] == now


@pytest.mark.asyncio
async def test_recovery_review_digest_binds_recent_observations(pg):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    candidate = await _recovery_candidate(pg, now)
    await _recovery_evidence(pg, candidate, now - timedelta(minutes=10))
    kwargs = dict(action="verify", review_ref="review:recent-digest", now=now)
    preview = await validator_operators.review_operator(candidate["id"], **kwargs)
    async with pg() as session:
        await session.execute(sa.update(validators_t).where(validators_t.c.id == candidate["id"]).values(heartbeat_window_samples=[]))
        await session.commit()
    with pytest.raises(validator_operators.OperatorReviewError, match="state changed"):
        await validator_operators.review_operator(candidate["id"], **kwargs, apply=True, expected_digest=preview["current_digest"])


@pytest.mark.asyncio
async def test_recovery_migration_preserves_history_and_backfills_no_samples(pg):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    candidate = await _recovery_candidate(pg, now)
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0035_validator_recovery_window.py"
    spec = importlib.util.spec_from_file_location("recovery_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = pg.kw["bind"]
    namespace = engine.sync_engine.get_execution_options()["schema_translate_map"][None]

    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()

    async with engine.begin() as connection:
        await connection.execute(sa.text(f'SET LOCAL search_path TO "{namespace}"'))
        await connection.run_sync(migrate)
    async with pg() as session:
        state = (await session.execute(sa.select(validators_t).where(validators_t.c.id == candidate["id"]))).mappings().one()
    assert state["heartbeat_window_started_at"] is None
    assert state["heartbeat_window_samples"] == []
    for key in (
        "account_id", "signing_wallet", "qualification_started_at", "heartbeat_sample_count",
        "operator_group_id", "independence_status",
    ):
        assert state[key] == candidate[key]
    assert validator_operators.qualification_metrics(dict(state), now=now)["coverage_basis"] == "since_enrollment"


@pytest.mark.asyncio
async def test_recent_coverage_cannot_verify_suspended_registration(pg):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    candidate = await _recovery_candidate(pg, now)
    await _recovery_evidence(pg, candidate, now - timedelta(minutes=10))
    async with pg() as session:
        await session.execute(sa.update(validators_t).where(validators_t.c.id == candidate["id"]).values(status="suspended"))
        await session.commit()
    kwargs = dict(action="verify", review_ref="review:inactive-recovery", now=now)
    preview = await validator_operators.review_operator(candidate["id"], **kwargs)
    assert "validator registration is not active" in preview["blocking_reasons"]
    with pytest.raises(validator_operators.OperatorReviewError, match="not active"):
        await validator_operators.review_operator(candidate["id"], **kwargs,
            apply=True, expected_digest=preview["current_digest"])
