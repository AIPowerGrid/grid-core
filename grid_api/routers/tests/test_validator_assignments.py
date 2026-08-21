# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
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

from grid_api import auth, database, safe_logging
from grid_api.ratelimit import limiter
from grid_api.routers import validator as validator_router
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import (
    metadata as v2_metadata,
    accounts as accounts_t,
    validator_assignments as assignments_t,
    validator_attestations as attestations_t,
    validator_probe_groups as probe_groups_t,
    validators as validators_t,
    workers as workers_t,
)


TEST_PRIVATE_KEY = "0x" + "01" * 32
TEST_WALLET = Account.from_key(TEST_PRIVATE_KEY).address.lower()


@pytest.fixture(autouse=True)
def _isolated_logging_salt(monkeypatch):
    monkeypatch.setenv("GRID_SALT", "validator-router-test-only-salt")
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    safe_logging._log_key.cache_clear()
    yield
    safe_logging._log_key.cache_clear()


def _sign(payload, private_key=TEST_PRIVATE_KEY):
    return Account.sign_message(
        encode_defunct(text=validators_svc._canonical(payload)),
        private_key=private_key,
    ).signature.hex()


async def _register(account_id, private_key=TEST_PRIVATE_KEY, capabilities=None):
    wallet = Account.from_key(private_key).address.lower()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts_t).values(id=account_id, wallet=wallet, flags={}),
        )
        await session.commit()
    payload = {
        "registration_schema": "aipg.validator.registration.v1",
        "validator": wallet,
        "software_version": "0.1.0-test",
        "capabilities": capabilities or ["text.basic.v1"],
        "ts": int(time.time()),
    }
    registered = await validators_svc.register_validator(
        account_id=account_id,
        account_wallet=wallet,
        payload=payload,
        signature=_sign(payload, private_key),
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
        probe_group_id=assignment["probe_group_id"],
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
async def test_registration_rejects_second_wallet_for_same_account(db):
    account_id = uuid.uuid4()
    await _register(account_id, "0x" + f"{7:064x}")
    replacement_key = "0x" + f"{8:064x}"
    replacement_wallet = Account.from_key(replacement_key).address.lower()
    async with await database.new_session() as session:
        await session.execute(
            sa.update(accounts_t)
            .where(accounts_t.c.id == account_id)
            .values(wallet=replacement_wallet)
        )
        await session.commit()
    payload = {
        "registration_schema": "aipg.validator.registration.v1",
        "validator": replacement_wallet,
        "software_version": "0.1.0-test",
        "capabilities": ["text.basic.v1"],
        "ts": int(time.time()),
    }

    with pytest.raises(validators_svc.RegistrationError, match="different signing wallet"):
        await validators_svc.register_validator(
            account_id=account_id,
            account_wallet=replacement_wallet,
            payload=payload,
            signature=_sign(payload, replacement_key),
        )


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
    assert stored["quorum_status"] == "pending"

    authoritative = await validators_svc.scorecards(authority="authoritative")
    assert authoritative["items"][0]["authority"] == "authoritative"
    assert authoritative["items"][0]["quorum_status"] == "pending"
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
    assert next_work["assignments"] == []


@pytest.mark.asyncio
async def test_validator_disagreement_marks_assignment_disputed(db):
    worker_id = str(uuid.uuid4())
    active_workers = [{
        "worker_id": worker_id,
        "name": "rig-shared",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
    }]
    records = []
    for key_int, verdict in ((2, "healthy"), (3, "failed")):
        private_key = "0x" + f"{key_int:064x}"
        wallet = Account.from_key(private_key).address.lower()
        account_id = uuid.uuid4()
        validator_id = await _register(account_id, private_key)
        issued = await validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=wallet,
            active_workers=active_workers,
            limit=1,
        )
        assignment = issued["assignments"][0]
        evidence_hash = f"{key_int}" * 64
        async with await database.new_session() as session:
            await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.id == assignment["assignment_id"])
                .values(
                    probe_status="completed",
                    probe_evidence_hash=evidence_hash,
                    probe_verdict="healthy",
                )
            )
            await session.commit()
        payload = _payload(
            validator=wallet,
            assignment_source="grid",
            assignment_id=assignment["assignment_id"],
            probe_group_id=assignment["probe_group_id"],
            grid_nonce=assignment["grid_nonce"],
            worker_id=worker_id,
            model=assignment["model"],
            modality=assignment["modality"],
            capability=assignment["capability"],
            canary_kind=assignment["canary_kind"],
            evidence_hash=evidence_hash,
            verdict=verdict,
        )
        stored = await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=payload,
            signature=_sign(payload, private_key),
        )
        records.append((assignment, stored))

    assert stored["authority"] == "authoritative"
    assert stored["quorum_status"] == "disputed"
    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(
                    probe_groups_t.c.status,
                    probe_groups_t.c.quorum_status,
                    probe_groups_t.c.quorum_outcome,
                ).where(probe_groups_t.c.id == records[0][0]["probe_group_id"])
            )
        ).mappings().one()
    assert row == {
        "status": "disputed",
        "quorum_status": "disputed",
        "quorum_outcome": "disputed",
    }
    health = await validators_svc.assignment_health()
    assert health["network"]["agreement_rate"] == 0.5
    assert health["network"]["disputed_rate"] == 1.0
    assert health["network"]["disputed_groups"] == 1


@pytest.mark.asyncio
async def test_three_distinct_registered_validators_accept_shared_probe_group(db):
    worker_id = str(uuid.uuid4())
    active_workers = [{
        "worker_id": worker_id,
        "name": "rig-quorum",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
    }]
    group_ids = set()
    statuses = []
    for key_int in (4, 5, 6):
        private_key = "0x" + f"{key_int:064x}"
        wallet = Account.from_key(private_key).address.lower()
        account_id = uuid.uuid4()
        validator_id = await _register(account_id, private_key)
        issued = await validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=wallet,
            active_workers=active_workers,
            limit=1,
        )
        assignment = issued["assignments"][0]
        assert "expected" not in assignment["challenge"]
        assert assignment["challenge"]["expected_hash"]
        group_ids.add(assignment["probe_group_id"])
        evidence_hash = f"{key_int}" * 64
        async with await database.new_session() as session:
            await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.id == assignment["assignment_id"])
                .values(
                    probe_status="completed",
                    probe_evidence_hash=evidence_hash,
                    probe_verdict="healthy",
                )
            )
            await session.commit()
        payload = _payload(
            validator=wallet,
            assignment_source="grid",
            assignment_id=assignment["assignment_id"],
            probe_group_id=assignment["probe_group_id"],
            grid_nonce=assignment["grid_nonce"],
            worker_id=worker_id,
            model=assignment["model"],
            modality=assignment["modality"],
            capability=assignment["capability"],
            canary_kind=assignment["canary_kind"],
            evidence_hash=evidence_hash,
            verdict="healthy",
        )
        stored = await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=payload,
            signature=_sign(payload, private_key),
        )
        statuses.append(stored["quorum_status"])

    assert group_ids == {assignment["probe_group_id"]}
    assert statuses == ["pending", "pending", "accepted"]
    async with await database.new_session() as session:
        group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == assignment["probe_group_id"])
            )
        ).mappings().one()
        votes = await session.scalar(
            sa.select(sa.func.count()).select_from(attestations_t).where(
                attestations_t.c.probe_group_id == assignment["probe_group_id"]
            )
        )
    assert group["quorum_status"] == "accepted"
    assert group["quorum_outcome"] == "healthy"
    assert votes == 3
    health = await validators_svc.assignment_health()
    assert health["stages"] == {
        "probes_completed": 3,
        "authoritative_evidence_accepted": 3,
        "workers_passed": 1,
        "quorum_reached": 1,
        "groups_finalized": 0,
    }
    assert health["validators"]["active"] == 3
    assert health["validators"]["heartbeat_fresh"] == 3
    assert health["validators"]["participating_24h"] == 3
    assert health["network"]["assignments_completed"] == 3
    assert health["network"]["groups_with_evidence"] == 1
    assert health["network"]["authoritative_votes"] == 3
    assert health["network"]["agreement_rate"] == 1.0
    assert health["network"]["disputed_rate"] == 0.0
    assert health["network"]["coverage"] == {"workers": 1, "models": 1}
    assert health["network"]["software_versions"] == [
        {"version": "0.1.0-test", "validators": 3}
    ]
    assert health["network"]["operator_independence"] == {
        "verified": 0,
        "proven": False,
        "status": "not_yet_verified",
    }

    dissent_key = "0x" + f"{9:064x}"
    dissent_wallet = Account.from_key(dissent_key).address.lower()
    dissent_account = uuid.uuid4()
    dissent_validator = await _register(dissent_account, dissent_key)
    dissent_assignment = (
        await validators_svc.issue_assignments(
            account_id=dissent_account,
            validator_id=dissent_validator,
            validator_wallet=dissent_wallet,
            active_workers=active_workers,
            limit=1,
        )
    )["assignments"][0]
    dissent_evidence = "9" * 64
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == dissent_assignment["assignment_id"])
            .values(
                probe_status="completed",
                probe_evidence_hash=dissent_evidence,
                probe_verdict="failed",
            )
        )
        await session.commit()
    dissent_payload = _payload(
        validator=dissent_wallet,
        assignment_source="grid",
        assignment_id=dissent_assignment["assignment_id"],
        probe_group_id=dissent_assignment["probe_group_id"],
        grid_nonce=dissent_assignment["grid_nonce"],
        worker_id=worker_id,
        model=dissent_assignment["model"],
        modality=dissent_assignment["modality"],
        capability=dissent_assignment["capability"],
        canary_kind=dissent_assignment["canary_kind"],
        evidence_hash=dissent_evidence,
        verdict="failed",
    )
    disputed = await validators_svc.record_attestation(
        account_id=dissent_account,
        validator_id=dissent_validator,
        payload=dissent_payload,
        signature=_sign(dissent_payload, dissent_key),
    )
    assert disputed["quorum_status"] == "disputed"
    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == dissent_assignment["probe_group_id"])
            .values(
                expires=validators_svc._now()
                - timedelta(seconds=validators_svc.ATTESTATION_GRACE_SECONDS + 1)
            )
        )
        await validators_svc._finalize_due_assignments(session)
        await session.commit()
        finalized = (
            await session.execute(
                sa.select(probe_groups_t).where(
                    probe_groups_t.c.id == dissent_assignment["probe_group_id"]
                )
            )
        ).mappings().one()
    assert finalized["quorum_status"] == "finalized"
    assert finalized["quorum_outcome"] == "healthy"


@pytest.mark.asyncio
async def test_assignment_groups_require_matching_validator_scorer_capability(db):
    worker_id = str(uuid.uuid4())
    active_workers = [{
        "worker_id": worker_id,
        "name": "rig-capabilities",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
    }]

    rich_key = "0x" + f"{30:064x}"
    rich_wallet = Account.from_key(rich_key).address.lower()
    rich_account = uuid.uuid4()
    rich_validator = await _register(
        rich_account,
        rich_key,
        capabilities=["text.structured.v1"],
    )
    rich = await validators_svc.issue_assignments(
        account_id=rich_account,
        validator_id=rich_validator,
        validator_wallet=rich_wallet,
        active_workers=active_workers,
        limit=1,
    )
    rich_assignment = rich["assignments"][0]
    assert rich_assignment["capability"] == "text.structured.v1"
    assert rich_assignment["canary_kind"] == "json.object"

    legacy_key = "0x" + f"{31:064x}"
    legacy_wallet = Account.from_key(legacy_key).address.lower()
    legacy_account = uuid.uuid4()
    legacy_validator = await _register(legacy_account, legacy_key)
    legacy = await validators_svc.issue_assignments(
        account_id=legacy_account,
        validator_id=legacy_validator,
        validator_wallet=legacy_wallet,
        active_workers=active_workers,
        limit=1,
    )
    legacy_assignment = legacy["assignments"][0]

    assert legacy_assignment["probe_group_id"] != rich_assignment["probe_group_id"]
    assert legacy_assignment["capability"] in {
        "text.instruction.v1",
        "text.reasoning.v1",
    }
    assert legacy_assignment["canary_kind"] in {"echo", "math.add", "math.mul"}


@pytest.mark.asyncio
async def test_completed_probe_can_deliver_during_attestation_grace(db):
    account_id = uuid.uuid4()
    validator_id, assignment, payload = await _assignment(account_id, verdict="healthy")
    async with await database.new_session() as session:
        expired = validators_svc._now() - timedelta(seconds=1)
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(expires=expired)
        )
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == assignment["probe_group_id"])
            .values(expires=expired)
        )
        await session.commit()

    stored = await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=payload,
        signature=_sign(payload),
    )
    assert stored["status"] == "accepted"
    assert stored["quorum_status"] == "pending"


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


@pytest.mark.asyncio
async def test_tool_call_probe_forwards_schema_and_commits_witnessed_call(db, monkeypatch):
    from grid_api.services import job_queue, token_stream

    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["text.tool_call.v1"])
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": str(uuid.uuid4()), "name": "rig-tool",
            "models": ["qwen3-27b"], "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    function = challenge["tools"][0]["function"]
    number_field, token_field = list(function["parameters"]["properties"])
    prompt = challenge["prompt"]
    expected_number = int(prompt.split(f"{number_field!r} to ", 1)[1].split(" and ", 1)[0])
    expected_token = prompt.split(f"{token_field!r} to '", 1)[1].split("'", 1)[0]
    tool_calls = [{
        "id": "call_witnessed", "type": "function",
        "function": {
            "name": function["name"],
            "arguments": json.dumps({number_field: expected_number, token_field: expected_token}),
        },
    }]
    submitted = {}

    async def capture_submit(job_id, payload, models, **kwargs):
        submitted.update(job_id=job_id, payload=payload, models=models, kwargs=kwargs)

    async def completed_events(*_args, **_kwargs):
        yield {
            "text": token_stream.DONE_SENTINEL, "full_text": "", "tool_calls": tool_calls,
            "finish_reason": "tool_calls", "usage": {"completion_tokens": 12},
            "grid": {"worker": "rig-tool"},
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert result["tool_calls"] == tool_calls
    assert result["finish_reason"] == "tool_calls"
    assert submitted["payload"]["request"]["tools"] == challenge["tools"]
    assert submitted["payload"]["request"]["tool_choice"] == challenge["tool_choice"]
    assert submitted["kwargs"]["hard_target_worker"] == "rig-tool"
    async with await database.new_session() as session:
        row = (await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment["assignment_id"])
        )).mappings().one()
    assert row["probe_status"] == "completed"
    assert row["probe_verdict"] == "healthy"
    assert row["probe_response_hash"] == result["response_hash"]


@pytest.mark.asyncio
async def test_tool_chain_probe_runs_two_hard_targeted_stages_and_commits_transcript(
    db, monkeypatch
):
    from grid_api.services import job_queue, token_stream

    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["text.tool_chain.v1"])
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": str(uuid.uuid4()), "name": "rig-chain",
            "models": ["qwen3-27b"], "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    first_fn = challenge["steps"][0]["tools"][0]["function"]
    second_fn = challenge["steps"][1]["tools"][0]["function"]
    lookup_field = next(iter(first_fn["parameters"]["properties"]))
    lookup_value = challenge["prompt"].split(
        f"{lookup_field!r} set to '", 1
    )[1].split("'", 1)[0]
    tool_result = challenge["steps"][1]["tool_result"]
    total_field, token_field = second_fn["parameters"]["properties"]
    first_calls = [{
        "id": "call_lookup", "type": "function",
        "function": {
            "name": first_fn["name"],
            "arguments": json.dumps({lookup_field: lookup_value}),
        },
    }]
    second_calls = [{
        "id": "call_submit", "type": "function",
        "function": {
            "name": second_fn["name"],
            "arguments": json.dumps({
                total_field: tool_result["left"] + tool_result["right"],
                token_field: tool_result["token"],
            }),
        },
    }]
    submitted = []

    async def capture_submit(job_id, payload, models, **kwargs):
        submitted.append({"job_id": job_id, "payload": payload, "models": models, **kwargs})

    async def completed_events(*_args, **_kwargs):
        calls = first_calls if len(submitted) == 1 else second_calls
        yield {
            "text": token_stream.DONE_SENTINEL,
            "full_text": "",
            "tool_calls": calls,
            "finish_reason": "tool_calls",
            "usage": {"completion_tokens": 12},
            "grid": {"worker": "rig-chain"},
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert result["tool_chain"][0]["tool_calls"] == first_calls
    assert result["tool_chain"][1]["tool_calls"] == second_calls
    assert len(submitted) == 2
    assert all(item["hard_target_worker"] == "rig-chain" for item in submitted)
    assert submitted[0]["payload"]["request"]["tools"] == challenge["steps"][0]["tools"]
    second_messages = submitted[1]["payload"]["request"]["messages"]
    assert second_messages[1]["tool_calls"] == first_calls
    assert json.loads(second_messages[2]["content"]) == tool_result
    assert submitted[1]["payload"]["request"]["tools"] == challenge["steps"][1]["tools"]
    async with await database.new_session() as session:
        row = (await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment["assignment_id"])
        )).mappings().one()
    assert row["probe_status"] == "completed"
    assert row["probe_verdict"] == "healthy"
    assert row["probe_response_hash"] == result["response_hash"]


@pytest.mark.asyncio
async def test_stop_sequence_is_exposed_and_forwarded_to_the_targeted_request(db, monkeypatch):
    from grid_api.services import job_queue

    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["text.stop_sequence.v1"])
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": str(uuid.uuid4()), "name": "rig-stop",
            "models": ["qwen3-27b"], "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    submitted = {}

    async def capture_submit(_job_id, payload, _models, **_kwargs):
        submitted.update(payload)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "error"
    assert assignment["challenge"]["stop"]
    assert submitted["request"]["stop"] == assignment["challenge"]["stop"]


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
    assert body["probe_policy"]["max_attempts"] >= 1
    assert body["probe_policy"]["lease_seconds"] > validators_svc.PROBE_TIMEOUT_SECONDS
    assert (
        body["authority_model"]["authoritative"]
        == "requires Grid-issued assignment_id + grid_nonce + probe evidence hash"
    )
    assert body["quorum_policy"]["threshold"] == 3
    assert body["quorum_policy"]["target_validators"] == 5
    assert body["quorum_policy"]["operator_independence_proven"] is False


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
