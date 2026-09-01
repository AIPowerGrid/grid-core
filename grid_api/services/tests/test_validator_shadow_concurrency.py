# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres idempotency proof for append-only shadow observations.

Set VALIDATOR_SHADOW_TEST_DB_URL to a disposable PostgreSQL database. The test
drops Grid metadata on exit and must never point at production.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import validator_shadow as shadow
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_shadow_observations as observations_t
from grid_api.v2.schema import validators as validators_t

PG_URL = os.environ.get("VALIDATOR_SHADOW_TEST_DB_URL", "")
NOW = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)
RUN_ID = "shadow_pg_concurrency"

pytestmark = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"),
    reason="set VALIDATOR_SHADOW_TEST_DB_URL to a disposable PostgreSQL database",
)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    engine = create_async_engine(PG_URL)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old_factory = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(
        shadow,
        "get_settings",
        lambda: SimpleNamespace(
            validator_shadow_observer_enabled=True,
            validator_cohort_baseline_version="v0.1.0-preview.13",
            validator_shadow_sample_seconds=300,
            validator_shadow_route_hmac_secret=SecretStr("s" * 32),
        ),
    )
    monkeypatch.setattr(shadow, "_now", lambda: NOW)

    async def fake_live_gate(**kwargs):
        snapshot = {
            "schema": "aipg.validator.shadow-live-start-gate.v1",
            "observed_at": (kwargs.get("observed_at") or NOW).isoformat(),
            **_gate(),
        }
        return {**snapshot, "evaluation": shadow.evaluate_start_gate(snapshot)}

    monkeypatch.setattr(shadow, "live_start_gate_snapshot", fake_live_gate)
    try:
        yield engine
    finally:
        database._session_factory = old_factory
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


def _gate():
    return {
        "verified_independent_operators": 3,
        "participating_independent_operators": 3,
        "finalized_independent_probe_groups": 1,
        "cohort_monitor_status": "healthy",
        "unresolved_critical_incidents": False,
        "postgres_migration_verified": True,
        "postgres_concurrency_verified": True,
        "replay_verified": True,
        "no_side_effect_verified": True,
        "routing_effect": "none",
        "economic_effect": "none",
    }


def _evidence():
    return {
        "group_commitment": "a" * 64,
        "worker_id": "worker-a",
        "model": "model-a",
        "modality": "text",
        "capability": "text.instruction.v1",
        "scoring_policy_id": "text.generated.v8",
        "evidence_dimension": "protocol_conformance",
        "quorum_status": "finalized",
        "outcome": "healthy",
        "distinct_operator_count": 3,
        "bindings_valid": True,
        "finalized_at": NOW - timedelta(minutes=5),
    }


async def _start():
    await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/postgres",
        verification=_gate(),
        observed_at=NOW,
    )
    await shadow.start_run(RUN_ID, started_at=NOW)


def _kwargs(*, task_class: str = "simple"):
    return {
        "run_id": RUN_ID,
        "route_ref": "b" * 64,
        "job_ref": "c" * 64,
        "task_class": task_class,
        "modality": "text",
        "requested_capability": "text.instruction.v1",
        "candidates": [
            {"worker_id": "worker-a", "model": "model-a", "baseline_rank": 0},
        ],
        "evidence": [_evidence()],
        "actual_model": "model-a",
        "actual_worker_id": "worker-a",
        "observed_at": NOW + timedelta(minutes=1),
    }


@pytest.mark.asyncio
async def test_duplicate_observation_race_appends_exactly_once(pg):
    await _start()
    results = await asyncio.gather(
        *[shadow._record_observation(**_kwargs()) for _ in range(20)],
    )
    assert len({row["id"] for row in results}) == 1
    assert len({row["payload_hash"] for row in results}) == 1
    async with await database.new_session() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(observations_t))
    assert count == 1


@pytest.mark.asyncio
async def test_conflicting_observation_race_never_rewrites_winner(pg):
    await _start()
    results = await asyncio.gather(
        *[
            shadow._record_observation(
                **_kwargs(task_class="simple" if index % 2 == 0 else "code"),
            )
            for index in range(20)
        ],
        return_exceptions=True,
    )
    successes = [row for row in results if isinstance(row, dict)]
    conflicts = [row for row in results if isinstance(row, shadow.ShadowConflict)]
    assert successes
    assert conflicts
    assert len({row["payload_hash"] for row in successes}) == 1
    async with await database.new_session() as session:
        rows = (await session.execute(sa.select(observations_t))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["payload_hash"] == successes[0]["payload_hash"]


@pytest.mark.asyncio
async def test_concurrent_shadow_starts_have_one_winner(pg):
    run_ids = ("shadow_pg_start_one", "shadow_pg_start_two")
    for run_id in run_ids:
        await shadow.create_run(
            run_id=run_id,
            policy_config=None,
            implementation_commit="a" * 40,
            verification_ref=f"ci://validator-shadow/{run_id}",
            verification=_gate(),
            observed_at=NOW,
        )
    results = await asyncio.gather(
        *(shadow.start_run(run_id, started_at=NOW) for run_id in run_ids),
        return_exceptions=True,
    )
    assert len([row for row in results if isinstance(row, dict)]) == 1
    assert len([row for row in results if isinstance(row, shadow.ShadowConflict)]) == 1


@pytest.mark.asyncio
async def test_postgres_live_capacity_applies_real_operator_eligibility_query(pg):
    async with await database.new_session() as session:
        for index in range(1, 5):
            account_id = UUID(int=index)
            await session.execute(
                sa.insert(accounts_t).values(
                    id=account_id,
                    flags={},
                    created=NOW - timedelta(days=5),
                ),
            )
            await session.execute(
                sa.insert(validators_t).values(
                    id=f"val_pg_{index}",
                    account_id=account_id,
                    signing_wallet="0x" + f"{index:040x}",
                    software_version="v0.1.0-preview.13",
                    capabilities=["text"],
                    registration_signature="0x" + (f"{index:x}" * 130)[:130],
                    status="active",
                    last_heartbeat=NOW - timedelta(minutes=1),
                    operator_group_id=(f"opg_pg_{index}" if index <= 3 else "public_group"),
                    independence_status="verified",
                    qualification_started_at=NOW - timedelta(days=4),
                    heartbeat_sample_count=900,
                    last_heartbeat_sampled_at=NOW - timedelta(minutes=1),
                    independence_reviewed_at=NOW - timedelta(days=1),
                    independence_expires_at=NOW + timedelta(days=30),
                    independence_review_ref=f"review/pg/{index}",
                    created=NOW - timedelta(days=4),
                    updated=NOW - timedelta(minutes=1),
                ),
            )
        await session.commit()

    capacity = await shadow.live_capacity_snapshot(observed_at=NOW)
    assert capacity["verified_independent_operators"] == 3
    assert capacity["participating_independent_operators"] == 0
    assert capacity["finalized_independent_probe_groups"] == 0
