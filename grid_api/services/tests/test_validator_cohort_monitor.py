# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services.validator_cohort_monitor import evaluate_snapshot, inspect_cohort_health
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_assignments as assignments_t
from grid_api.v2.schema import validator_attestations as attestations_t
from grid_api.v2.schema import validators as validators_t

NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old_factory = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield
    finally:
        database._session_factory = old_factory
        await engine.dispose()


def _snapshot():
    return {
        "schema": "aipg.validator.cohort-monitor.v1",
        "generated_at": "2026-08-31T16:00:00+00:00",
        "window_hours": 24,
        "baseline_version": "v0.1.0-preview.13",
        "assignments": {
            "matured": 100,
            "completed": 98,
            "terminal_failures": 2,
            "authoritative_evidence": 95,
        },
        "validators": {
            "active": 3,
            "fresh": 3,
            "stale_active": 0,
            "candidates": 3,
            "stale_candidates": 0,
            "candidates_ready": 0,
            "fresh_verified": 0,
            "fresh_outdated": 0,
            "duplicate_control_groups": 0,
            "software_versions": [
                {"version": "v0.1.0-preview.13", "validators": 3},
            ],
        },
        "network": {
            "groups_with_evidence": 20,
            "authoritative_votes": 60,
            "agreement_rate": 0.98,
            "disputed_groups": 1,
            "disputed_rate": 0.05,
        },
    }


def test_healthy_snapshot_is_non_economic_and_computes_rates():
    report = evaluate_snapshot(_snapshot())

    assert report["status"] == "healthy"
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["economic_effect"] == "none"
    assert report["assignments"]["completion_rate"] == 0.98
    assert report["assignments"]["evidence_rate"] == 95 / 98
    assert report["assignments"]["probe_error_rate"] == 0.02


def test_snapshot_classifies_cohort_regressions_without_identifiers():
    snapshot = deepcopy(_snapshot())
    snapshot["assignments"].update(
        {
            "matured": 100,
            "completed": 70,
            "terminal_failures": 20,
            "authoritative_evidence": 50,
        },
    )
    snapshot["validators"].update(
        {
            "active": 5,
            "fresh": 2,
            "stale_active": 3,
            "candidates": 2,
            "stale_candidates": 1,
            "candidates_ready": 1,
            "fresh_outdated": 1,
            "duplicate_control_groups": 1,
        },
    )
    snapshot["network"].update(
        {
            "groups_with_evidence": 10,
            "disputed_groups": 3,
            "disputed_rate": 0.3,
        },
    )

    report = evaluate_snapshot(snapshot)
    issues = {issue["code"]: issue for issue in report["issues"]}

    assert report["status"] == "critical"
    assert set(issues) == {
        "low_assignment_completion",
        "low_evidence_submission",
        "high_probe_error_rate",
        "candidate_heartbeat_stale",
        "candidate_ready_for_review",
        "active_validators_stale",
        "validator_version_drift",
        "duplicate_control_groups",
        "high_validator_disagreement",
    }
    serialized = repr(report).lower()
    assert "validator_id" not in serialized
    assert "operator_group_id" not in serialized
    assert "wallet" not in serialized
    assert "review_ref" not in serialized


def test_small_sample_is_informational_not_a_false_failure():
    snapshot = deepcopy(_snapshot())
    snapshot["assignments"].update(
        {
            "matured": 4,
            "completed": 0,
            "terminal_failures": 4,
            "authoritative_evidence": 0,
        },
    )

    report = evaluate_snapshot(snapshot)

    assert report["status"] == "healthy"
    assert [issue["code"] for issue in report["issues"]] == [
        "insufficient_matured_sample",
    ]


@pytest.mark.asyncio
async def test_inspect_cohort_health_uses_only_matured_assignments_and_distinct_evidence(db):
    account_id = uuid4()
    validator_id = "val_monitor_integration_0001"
    wallet = "0x" + "1" * 40
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, wallet=wallet, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet=wallet,
                software_version="v0.1.0-preview.13",
                capabilities=["text.generated.v8"],
                registration_signature="0x" + "11" * 65,
                status="active",
                last_heartbeat=NOW,
                operator_group_id="opg_monitor_integration",
                independence_status="candidate",
                qualification_started_at=NOW - timedelta(hours=1),
                created=NOW - timedelta(days=1),
                updated=NOW,
            ),
        )
        common = {
            "account_id": account_id,
            "validator_wallet": wallet,
            "validator_id": validator_id,
            "target_worker_id": "worker-monitor",
            "target_worker_name": "worker-monitor",
            "model": "test-model",
            "modality": "text",
            "capability": "text.generated.v8",
            "canary_kind": "math",
            "scoring_policy_id": "text.generated.v8",
            "challenge": {},
            "status": "pending",
            "quorum_status": "pending",
            "probe_attempts": 1,
            "created": NOW - timedelta(hours=2),
            "probed": None,
        }
        await session.execute(
            sa.insert(assignments_t),
            [
                {
                    **common,
                    "id": "asg_completed",
                    "grid_nonce": "nonce-completed",
                    "probe_status": "completed",
                    "expires": NOW - timedelta(hours=1),
                    "probed": NOW - timedelta(hours=1),
                },
                {
                    **common,
                    "id": "asg_failed",
                    "grid_nonce": "nonce-failed",
                    "probe_status": "failed",
                    "expires": NOW - timedelta(minutes=30),
                },
                {
                    **common,
                    "id": "asg_not_matured",
                    "grid_nonce": "nonce-live",
                    "probe_status": "not_started",
                    "expires": NOW + timedelta(minutes=10),
                },
            ],
        )
        await session.execute(
            sa.insert(attestations_t).values(
                attestation_hash="a" * 64,
                account_id=account_id,
                validator_wallet=wallet,
                validator_id=validator_id,
                assignment_id="asg_completed",
                grid_nonce="nonce-completed",
                evidence_hash="b" * 64,
                authority="authoritative",
                quorum_status="pending",
                worker_id="worker-monitor",
                model="test-model",
                modality="text",
                capability="text.generated.v8",
                canary_kind="math",
                nonce="nonce-completed",
                verdict="healthy",
                score=1.0,
                signature="0x" + "22" * 65,
                signature_status="verified",
                payload={},
                created=NOW - timedelta(minutes=50),
            ),
        )
        await session.commit()

    report = await inspect_cohort_health(now=NOW)

    assert report["assignments"] == {
        "matured": 2,
        "completed": 1,
        "terminal_failures": 1,
        "authoritative_evidence": 1,
        "completion_rate": 0.5,
        "evidence_rate": 1.0,
        "probe_error_rate": 0.5,
    }
    assert report["validators"]["candidates"] == 1
    assert report["validators"]["stale_candidates"] == 0
    assert report["validators"]["candidates_ready"] == 0
    assert report["validators"]["duplicate_control_groups"] == 0
    assert report["network"] == {
        "groups_with_evidence": 0,
        "authoritative_votes": 0,
        "agreement_rate": None,
        "disputed_groups": 0,
        "disputed_rate": None,
    }
    assert report["economic_effect"] == "none"
