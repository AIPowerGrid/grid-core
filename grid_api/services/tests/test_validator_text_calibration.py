# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services.validator_text_calibration import inspect_text_calibration
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_assignments as assignments_t

NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_report_is_aggregate_non_economic_and_redacts_evidence(db):
    account_id = uuid4()
    common = {
        "account_id": account_id,
        "target_worker_id": "worker-private-id",
        "target_worker_name": "private worker name",
        "model": "public-model",
        "modality": "text",
        "capability": "text.stop_sequence.v1",
        "canary_kind": "stop.sequence",
        "scoring_policy_id": "text.generated.v8",
        "status": "pending",
        "quorum_status": "pending",
        "probe_attempts": 1,
        "created": NOW - timedelta(hours=1),
        "expires": NOW + timedelta(hours=1),
    }
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts_t).values(id=account_id, flags={}),
        )
        await session.execute(
            sa.insert(assignments_t),
            [
                {
                    **common,
                    "id": "asg_private_completed",
                    "grid_nonce": "private-grid-nonce",
                    "challenge": {"prompt": "PRIVATE CHALLENGE"},
                    "probe_status": "completed",
                    "probe_verdict": "failed",
                    "probe_latency_ms": 1234,
                    "probe_result": {
                        "score_reason": "empty_visible_output",
                        "finish_reason": "stop",
                        "output_text": "PRIVATE OUTPUT",
                        "evidence_hash": "private-evidence",
                    },
                },
                {
                    **common,
                    "id": "asg_legacy_completed",
                    "grid_nonce": "legacy-grid-nonce",
                    "challenge": {"prompt": "ANOTHER PRIVATE CHALLENGE"},
                    "probe_status": "completed",
                    "probe_verdict": "healthy",
                    "probe_latency_ms": 100,
                    "probe_result": {},
                },
                {
                    **common,
                    "id": "asg_transport_failed",
                    "grid_nonce": "transport-grid-nonce",
                    "challenge": {"prompt": "THIRD PRIVATE CHALLENGE"},
                    "probe_status": "failed",
                    "probe_verdict": None,
                    "probe_latency_ms": None,
                    "probe_result": None,
                },
            ],
        )
        await session.commit()

    async with await database.new_session() as session:
        report = await inspect_text_calibration(session, now=NOW)

    assert report["policy"] == {
        "advisory_only": True,
        "economic_effect": "none",
        "routing_effect": "none",
        "quality_authority": "none",
    }
    assert report["observations"] == [
        {
            "capability": "text.stop_sequence.v1",
            "canary_kind": "stop.sequence",
            "model": "public-model",
            "score_dimension": "protocol_conformance",
            "quality_eligible": False,
            "verdict": "failed",
            "score_reason": "empty_visible_output",
            "finish_reason": "stop",
            "count": 1,
            "avg_latency_ms": 1234.0,
        },
        {
            "capability": "text.stop_sequence.v1",
            "canary_kind": "stop.sequence",
            "model": "public-model",
            "score_dimension": "protocol_conformance",
            "quality_eligible": False,
            "verdict": "healthy",
            "score_reason": "legacy_unclassified",
            "finish_reason": "not_reported",
            "count": 1,
            "avg_latency_ms": 100.0,
        },
    ]
    assert report["transport"] == [
        {
            "canary_kind": "stop.sequence",
            "model": "public-model",
            "probe_status": "completed",
            "count": 2,
        },
        {
            "canary_kind": "stop.sequence",
            "model": "public-model",
            "probe_status": "failed",
            "count": 1,
        },
    ]
    serialized = json.dumps(report, sort_keys=True)
    for secret in (
        "PRIVATE CHALLENGE",
        "PRIVATE OUTPUT",
        "private-grid-nonce",
        "private-evidence",
        "worker-private-id",
        "private worker name",
        "asg_private_completed",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_report_clamps_window_and_excludes_old_rows(db):
    account_id = uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(assignments_t).values(
                id="asg_old",
                account_id=account_id,
                grid_nonce="nonce-old",
                target_worker_id="worker-old",
                target_worker_name="worker-old",
                model="old-model",
                modality="text",
                capability="text.instruction.v1",
                canary_kind="echo",
                scoring_policy_id="text.generated.v8",
                challenge={},
                status="pending",
                quorum_status="pending",
                probe_status="completed",
                probe_attempts=1,
                probe_verdict="healthy",
                probe_result={"score_reason": "accepted"},
                created=NOW - timedelta(hours=2),
                expires=NOW - timedelta(hours=1),
            ),
        )
        await session.commit()

    async with await database.new_session() as session:
        report = await inspect_text_calibration(session, window_hours=0, now=NOW)

    assert report["window_hours"] == 1
    assert report["observations"] == []
    assert report["transport"] == []
