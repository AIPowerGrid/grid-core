# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.ratelimit import limiter
from grid_api.routers import validator as validator_router
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import (
    metadata as v2_metadata,
    validator_assignments as assignments_t,
    validator_attestations as attestations_t,
    workers as workers_t,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


def _payload(**overrides):
    data = {
        "validator": "0x1111111111111111111111111111111111111111",
        "assignment_source": "validator_v0",
        "assignment_id": "validator-v0:local",
        "grid_nonce": "",
        "worker_id": "",
        "model": "qwen3-27b",
        "modality": "text",
        "capability": "text.basic.v0",
        "canary_kind": "echo",
        "nonce": "ABC123",
        "verdict": "healthy",
        "score": 1.0,
        "latency_ms": 1234,
        "ts": 1782490000,
    }
    data.update(overrides)
    return data


async def _assignment(account_id, *, verdict="healthy"):
    worker_id = str(uuid.uuid4())
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_wallet="0x1111111111111111111111111111111111111111",
        active_workers=[{
            "worker_id": worker_id,
            "name": "rig-1",
            "models": ["qwen3-27b"],
            "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    evidence_hash = "a" * 64
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(
                probe_status="completed",
                probe_prompt_hash="b" * 64,
                probe_response_hash="c" * 64,
                probe_evidence_hash=evidence_hash,
                probe_verdict=verdict,
                probe_latency_ms=1234,
            )
        )
        await session.commit()
    payload = _payload(
        assignment_source="grid",
        assignment_id=assignment["assignment_id"],
        grid_nonce=assignment["grid_nonce"],
        worker_id=assignment["target_worker_id"],
        model=assignment["model"],
        modality=assignment["modality"],
        capability=assignment["capability"],
        canary_kind=assignment["canary_kind"],
        evidence_hash=evidence_hash,
        verdict=verdict,
    )
    return assignment, payload


@pytest.mark.asyncio
async def test_preview_attestation_does_not_affect_authoritative_scorecards(db):
    account_id = uuid.uuid4()
    stored = await validators_svc.record_attestation(
        account_id=account_id,
        payload=_payload(),
        signature=None,
    )

    assert stored["authority"] == "preview"
    assert stored["assignment_id"] is None

    authoritative = await validators_svc.scorecards(authority="authoritative")
    preview = await validators_svc.scorecards(authority="preview")

    assert authoritative["items"] == []
    assert preview["items"][0]["authority"] == "preview"
    assert preview["items"][0]["total"] == 1


@pytest.mark.asyncio
async def test_authoritative_attestation_requires_grid_assignment_and_nonce(db):
    account_id = uuid.uuid4()
    assignment, payload = await _assignment(account_id)

    bad = dict(payload)
    bad["grid_nonce"] = "wrong"
    with pytest.raises(validators_svc.AttestationError, match="grid_nonce"):
        await validators_svc.record_attestation(account_id=account_id, payload=bad, signature=None)

    wrong_evidence = dict(payload)
    wrong_evidence["evidence_hash"] = "d" * 64
    with pytest.raises(validators_svc.AttestationError, match="evidence_hash"):
        await validators_svc.record_attestation(
            account_id=account_id,
            payload=wrong_evidence,
            signature=None,
        )

    stored = await validators_svc.record_attestation(
        account_id=account_id,
        payload=payload,
        signature=None,
    )
    assert stored["authority"] == "authoritative"
    assert stored["assignment_id"] == assignment["assignment_id"]
    assert stored["quorum_status"] == "accepted"

    authoritative = await validators_svc.scorecards(authority="authoritative")
    assert authoritative["items"][0]["authority"] == "authoritative"
    assert authoritative["items"][0]["quorum_status"] == "accepted"
    assert authoritative["items"][0]["worker_id"] == assignment["target_worker_id"]

    next_work = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_wallet=None,
        active_workers=[{
            "worker_id": assignment["target_worker_id"],
            "name": assignment["target_worker_name"],
            "models": [assignment["model"]],
            "job_types": ["text"],
        }],
        limit=1,
    )
    assert next_work["assignments"][0]["assignment_id"] != assignment["assignment_id"]


@pytest.mark.asyncio
async def test_conflicting_authoritative_attestations_mark_assignment_disputed(db):
    account_id = uuid.uuid4()
    assignment, payload = await _assignment(account_id, verdict="healthy")
    await validators_svc.record_attestation(account_id=account_id, payload=payload, signature=None)

    conflict = dict(payload)
    conflict["verdict"] = "failed"
    stored = await validators_svc.record_attestation(
        account_id=account_id,
        payload=conflict,
        signature=None,
    )

    assert stored["quorum_status"] == "disputed"
    health = await validators_svc.assignment_health(account_id=account_id)
    assert health["quorum"]["disputed"] == 1
    assert health["recent"][0]["assignment_id"] == assignment["assignment_id"]
    assert "grid_nonce" not in health["recent"][0]
    assert "challenge" not in health["recent"][0]


@pytest.mark.asyncio
async def test_issue_assignments_excludes_validator_owned_workers(db):
    account_id = uuid.uuid4()
    own_worker_id = uuid.uuid4()
    other_worker_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(workers_t).values(
                id=own_worker_id,
                account_id=account_id,
                name="own-rig",
                type="text",
                models=["qwen3-27b"],
                capabilities={"job_types": ["text"]},
            )
        )
        await session.commit()

    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_wallet=None,
        active_workers=[
            {
                "worker_id": str(own_worker_id),
                "name": "own-rig",
                "models": ["qwen3-27b"],
                "job_types": ["text"],
            },
            {
                "worker_id": str(other_worker_id),
                "name": "stranger-rig",
                "models": ["qwen3-27b"],
                "job_types": ["text"],
            },
        ],
        limit=5,
    )

    assert issued["count"] == 1
    assert issued["assignments"][0]["target_worker_id"] == str(other_worker_id)
    assert issued["assignments"][0]["grid_nonce"]


def test_validator_capabilities_expose_assignment_gates():
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(validator_router.router)

    with TestClient(app) as client:
        resp = client.get("/v1/validator/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert body["economic_effect"] == "none"
    assert body["features"]["assignments"] is True
    assert body["features"]["targeted_probe"] is True
    assert body["features"]["quorum"] is True
    assert body["features"]["validator_rewards"] is False
    assert (
        body["authority_model"]["authoritative"]
        == "requires Grid-issued assignment_id + grid_nonce + probe evidence hash"
    )


def test_probe_route_returns_upstream_probe_error(monkeypatch):
    account_id = uuid.uuid4()

    async def fake_auth(_key):
        return {"source": "v2", "account_id": account_id, "wallet": None}

    async def fake_probe(**kwargs):
        assert kwargs["account_id"] == account_id
        assert kwargs["assignment_id"] == "asg_dead"
        return {"status": "error", "code": 503, "message": "target unavailable"}

    monkeypatch.setattr(validator_router.accounts_svc, "authenticate", fake_auth)
    monkeypatch.setattr(validator_router.validators_svc, "probe_assignment", fake_probe)

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(validator_router.router)

    with TestClient(app) as client:
        resp = client.post("/v1/validator/probe/asg_dead", headers={"apikey": "k"})

    assert resp.status_code == 503
    assert resp.json()["message"] == "target unavailable"
