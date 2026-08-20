# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import uuid
import time
from datetime import timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import auth, database
from grid_api.ratelimit import limiter
from grid_api.routers import validator as validator_router
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import (
    metadata as v2_metadata,
    accounts as accounts_t,
    validator_assignments as assignments_t,
    validator_attestations as attestations_t,
    validators as validators_t,
    workers as workers_t,
)


TEST_PRIVATE_KEY = "0x" + "01" * 32
TEST_WALLET = Account.from_key(TEST_PRIVATE_KEY).address.lower()


def _sign(payload):
    return Account.sign_message(
        encode_defunct(text=validators_svc._canonical(payload)),
        private_key=TEST_PRIVATE_KEY,
    ).signature.hex()


async def _register(account_id):
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts_t).values(id=account_id, wallet=TEST_WALLET, flags={}),
        )
        await session.commit()
    payload = {
        "registration_schema": "aipg.validator.registration.v1",
        "validator": TEST_WALLET,
        "software_version": "0.1.0-test",
        "capabilities": ["text.basic.v1"],
        "ts": int(time.time()),
    }
    registered = await validators_svc.register_validator(
        account_id=account_id,
        account_wallet=TEST_WALLET,
        payload=payload,
        signature=_sign(payload),
    )
    return registered["validator_id"]


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
        "validator": TEST_WALLET,
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
    validator_id = await _register(account_id)
    worker_id = str(uuid.uuid4())
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
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
    return validator_id, assignment, payload


async def _fresh_assignment(account_id):
    validator_id = await _register(account_id)
    worker_id = str(uuid.uuid4())
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": worker_id,
            "name": "rig-lease",
            "models": ["qwen3-27b"],
            "job_types": ["text"],
        }],
        limit=1,
    )
    return validator_id, issued["assignments"][0]


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
async def test_registration_is_wallet_bound_signed_and_idempotent(db):
    account_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts_t).values(id=account_id, wallet=TEST_WALLET, flags={}),
        )
        await session.commit()
    payload = {
        "registration_schema": "aipg.validator.registration.v1",
        "validator": TEST_WALLET,
        "software_version": "0.1.0-preview",
        "capabilities": ["text.basic.v1"],
        "ts": int(time.time()),
    }

    first = await validators_svc.register_validator(
        account_id=account_id,
        account_wallet=TEST_WALLET,
        payload=payload,
        signature=_sign(payload),
    )
    second = await validators_svc.register_validator(
        account_id=account_id,
        account_wallet=TEST_WALLET,
        payload=payload,
        signature=_sign(payload),
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["validator_id"] == first["validator_id"]
    async with await database.new_session() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(validators_t))
    assert count == 1


@pytest.mark.asyncio
async def test_registration_rejects_unlinked_wallet_unsigned_and_stale(db):
    account_id = uuid.uuid4()
    payload = {
        "registration_schema": "aipg.validator.registration.v1",
        "validator": TEST_WALLET,
        "software_version": "0.1.0-preview",
        "capabilities": ["text.basic.v1"],
        "ts": int(time.time()),
    }
    with pytest.raises(validators_svc.RegistrationError, match="linked wallet"):
        await validators_svc.register_validator(
            account_id=account_id,
            account_wallet=None,
            payload=payload,
            signature=_sign(payload),
        )
    with pytest.raises(validators_svc.RegistrationError, match="requires a wallet signature"):
        await validators_svc.register_validator(
            account_id=account_id,
            account_wallet=TEST_WALLET,
            payload=payload,
            signature=None,
        )
    stale = {**payload, "ts": 1}
    with pytest.raises(validators_svc.RegistrationError, match="outside the allowed window"):
        await validators_svc.register_validator(
            account_id=account_id,
            account_wallet=TEST_WALLET,
            payload=stale,
            signature=_sign(stale),
        )


@pytest.mark.asyncio
async def test_validator_key_has_only_validator_scopes(db, monkeypatch):
    monkeypatch.setenv("GRID_SALT", "validator-test-salt")
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    account_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.commit()

    key = await validator_router.accounts_svc.issue_key(
        account_id,
        label="validator",
        scopes=validator_router.accounts_svc.VALIDATOR_SCOPES,
        key_kind="validator",
    )
    resolved = await validator_router.accounts_svc.resolve_api_key(key)

    assert resolved["key_kind"] == "validator"
    assert resolved["scopes"] == validator_router.accounts_svc.VALIDATOR_SCOPES
    assert "inference.submit" not in resolved["scopes"]


@pytest.mark.asyncio
async def test_authoritative_attestation_requires_grid_assignment_and_nonce(db):
    account_id = uuid.uuid4()
    validator_id, assignment, payload = await _assignment(account_id)

    with pytest.raises(validators_svc.AttestationError, match="verified validator signature"):
        await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=payload,
            signature=None,
        )

    bad = dict(payload)
    bad["grid_nonce"] = "wrong"
    with pytest.raises(validators_svc.AttestationError, match="grid_nonce"):
        await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=bad,
            signature=_sign(bad),
        )

    wrong_evidence = dict(payload)
    wrong_evidence["evidence_hash"] = "d" * 64
    with pytest.raises(validators_svc.AttestationError, match="evidence_hash"):
        await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=wrong_evidence,
            signature=_sign(wrong_evidence),
        )

    stored = await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=payload,
        signature=_sign(payload),
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
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
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
async def test_one_authoritative_attestation_per_registered_validator(db):
    account_id = uuid.uuid4()
    validator_id, assignment, payload = await _assignment(account_id, verdict="healthy")
    await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=payload,
        signature=_sign(payload),
    )

    conflict = dict(payload)
    conflict["verdict"] = "failed"
    with pytest.raises(validators_svc.AttestationError, match="already submitted"):
        await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=conflict,
            signature=_sign(conflict),
        )


@pytest.mark.asyncio
async def test_issue_assignments_excludes_validator_owned_workers(db):
    account_id = uuid.uuid4()
    validator_id = await _register(account_id)
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
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
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


@pytest.mark.asyncio
async def test_probe_lease_allows_only_one_concurrent_claim(db):
    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)

    results = await asyncio.gather(
        *[
            validators_svc._claim_probe_lease(
                account_id=account_id,
                validator_id=validator_id,
                assignment_id=assignment["assignment_id"],
            )
            for _ in range(8)
        ],
        return_exceptions=True,
    )

    winners = [result for result in results if isinstance(result, tuple)]
    rejected = [result for result in results if isinstance(result, Exception)]
    assert len(winners) == 1
    assert len(rejected) == 7
    assert all("already in progress" in str(error) for error in rejected)

    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
    assert row["probe_attempts"] == 1
    assert row["probe_status"] == "running"


@pytest.mark.asyncio
async def test_probe_lease_retry_budget_and_late_result_guard(db, monkeypatch):
    monkeypatch.setattr(validators_svc, "PROBE_MAX_ATTEMPTS", 2)
    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)

    _, first_job = await validators_svc._claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(probe_lease_expires=validators_svc._now() - timedelta(seconds=1))
        )
        await session.commit()

    _, second_job = await validators_svc._claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )
    assert second_job != first_job

    await validators_svc._mark_probe(
        first_job,
        "completed",
        evidence_hash="a" * 64,
    )
    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
    assert row["probe_job_id"] == second_job
    assert row["probe_status"] == "running"
    assert row["probe_evidence_hash"] is None
    assert row["probe_attempts"] == 2

    await validators_svc._mark_probe(second_job, "failed")
    with pytest.raises(validators_svc.AssignmentError, match="retry limit reached"):
        await validators_svc._claim_probe_lease(
            account_id=account_id,
            validator_id=validator_id,
            assignment_id=assignment["assignment_id"],
        )


@pytest.mark.asyncio
async def test_probe_dispatch_failure_releases_lease_for_retry(db, monkeypatch):
    from grid_api.services import job_queue

    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(job_queue, "submit_job", fail_dispatch)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "error"
    assert result["code"] == 503
    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
    assert row["probe_status"] == "failed"
    assert row["probe_lease_expires"] is None
    assert row["probe_attempts"] == 1


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
    assert body["features"]["quorum"] is False
    assert body["features"]["validator_rewards"] is False
    assert body["probe_policy"]["max_attempts"] >= 1
    assert body["probe_policy"]["lease_seconds"] > validators_svc.PROBE_TIMEOUT_SECONDS
    assert (
        body["authority_model"]["authoritative"]
        == "requires Grid-issued assignment_id + grid_nonce + probe evidence hash"
    )
    assert "not implemented" in body["authority_model"]["real_quorum"]


def test_probe_route_returns_upstream_probe_error(monkeypatch):
    account_id = uuid.uuid4()

    async def fake_auth(_key, *, required_scope):
        assert required_scope == "validator.probe"
        return {"source": "v2", "account_id": account_id, "wallet": TEST_WALLET}

    async def fake_active(**_kwargs):
        return {"id": "val_test"}

    async def fake_probe(**kwargs):
        assert kwargs["account_id"] == account_id
        assert kwargs["validator_id"] == "val_test"
        assert kwargs["assignment_id"] == "asg_dead"
        return {"status": "error", "code": 503, "message": "target unavailable"}

    monkeypatch.setattr(validator_router.accounts_svc, "authenticate", fake_auth)
    monkeypatch.setattr(validator_router.validators_svc, "active_validator", fake_active)
    monkeypatch.setattr(validator_router.validators_svc, "probe_assignment", fake_probe)

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(validator_router.router)

    with TestClient(app) as client:
        resp = client.post("/v1/validator/probe/asg_dead", headers={"apikey": "k"})

    assert resp.status_code == 503
    assert resp.json()["message"] == "target unavailable"


def test_probe_route_rejects_duplicate_assignment(monkeypatch):
    account_id = uuid.uuid4()

    async def fake_auth(_key, *, required_scope):
        assert required_scope == "validator.probe"
        return {"source": "v2", "account_id": account_id, "wallet": TEST_WALLET}

    async def fake_active(**_kwargs):
        return {"id": "val_test"}

    async def fake_probe(**_kwargs):
        raise validators_svc.AssignmentError("assignment probe already in progress")

    monkeypatch.setattr(validator_router.accounts_svc, "authenticate", fake_auth)
    monkeypatch.setattr(validator_router.validators_svc, "active_validator", fake_active)
    monkeypatch.setattr(validator_router.validators_svc, "probe_assignment", fake_probe)

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(validator_router.router)

    with TestClient(app) as client:
        resp = client.post("/v1/validator/probe/asg_busy", headers={"apikey": "k"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "assignment probe already in progress"
