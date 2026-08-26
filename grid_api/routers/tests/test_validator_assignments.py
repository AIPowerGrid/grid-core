# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import auth, database, safe_logging
from grid_api.ratelimit import limiter
from grid_api.routers import validator as validator_router
from grid_api.services import recipes
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import (
    accounts as accounts_t,
)
from grid_api.v2.schema import (
    metadata as v2_metadata,
)
from grid_api.v2.schema import (
    validator_assignments as assignments_t,
)
from grid_api.v2.schema import (
    validator_attestations as attestations_t,
)
from grid_api.v2.schema import (
    validator_probe_groups as probe_groups_t,
)
from grid_api.v2.schema import (
    validator_reference_workers as references_t,
)
from grid_api.v2.schema import (
    validators as validators_t,
)
from grid_api.v2.schema import (
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


def _media_settings(*, enabled=True, video_enabled=False):
    return SimpleNamespace(
        validator_media_probe_enabled=enabled,
        validator_video_probe_enabled=video_enabled,
        validator_media_bond_chain_id=8453,
        validator_media_bond_contract="0x" + "a" * 40,
        validator_media_bond_verifier_version="worker-registry-v2",
        validator_media_minimum_bond_raw=10**18,
        validator_media_minimum_quality_pass_rate=0.95,
        validator_media_max_output_bytes=25 * 1024 * 1024,
        validator_media_probe_timeout_seconds=600,
    )


async def _seed_image_worker(session, index, *, now):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    wallet = f"0x{index:040x}"
    await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
    await session.execute(
        sa.insert(workers_t).values(
            id=worker_id,
            account_id=account_id,
            name=f"image-rig-{index}",
            type="image",
            wallet=wallet,
            models=["deterministic-checkpoint"],
            capabilities={},
            maintenance=False,
            first_seen=now - timedelta(days=1),
            last_seen=now,
            jobs_completed=20,
            den_earned=0,
        )
    )
    return worker_id, account_id, wallet


async def _seed_image_reference(session, worker, *, now):
    worker_id, account_id, wallet = worker
    await session.execute(
        sa.insert(references_t).values(
            worker_id=worker_id,
            model="Deterministic Image",
            modality="image",
            account_id=account_id,
            payout_wallet=wallet,
            status="active",
            status_reason="test fixture",
            bond_contract="0x" + "a" * 40,
            bond_chain_id=8453,
            bond_finalized_block=123,
            bond_amount_raw=Decimal(10**18),
            bond_active=True,
            bond_slashed=False,
            bond_verifier_version="worker-registry-v2",
            bond_verified_at=now,
            quality_window_start=now - timedelta(days=1),
            quality_window_end=now,
            quality_pass_rate=0.99,
            quality_reviewed_at=now,
            selection_count=0,
            created=now,
            updated=now,
        )
    )


def _register_image_recipe(*, deterministic=True, recipe_id=42):
    recipes._BY_ROOT.clear()
    recipes._BY_ID.clear()
    recipes._BY_NAME.clear()
    recipes._BY_MODEL.clear()
    return recipes.register_recipe(
        "0x" + "b" * 64,
        "deterministic-image-test",
        {
            "_grid": {
                "engine": "comfyui",
                "modelName": "Deterministic Image",
                "jobType": "image",
                "deterministic": deterministic,
                "modelDigest": "c" * 64,
                "requiredModels": ["deterministic-checkpoint"],
                "vars": {
                    "prompt": "1.inputs.text",
                    "seed": "2.inputs.seed",
                    "width": "3.inputs.width",
                    "height": "3.inputs.height",
                    "steps": "2.inputs.steps",
                },
                "clamps": {
                    "width": [512, 1024],
                    "height": [512, 1024],
                    "steps": [4, 30],
                },
            },
            "1": {"inputs": {"text": ""}},
            "2": {"inputs": {"seed": 0, "steps": 12}},
            "3": {"inputs": {"width": 512, "height": 512}},
        },
        recipe_id=recipe_id,
    )


def _register_video_recipe(*, recipe_id=84, include_fps=True):
    recipes._BY_ROOT.clear()
    recipes._BY_ID.clear()
    recipes._BY_NAME.clear()
    recipes._BY_MODEL.clear()
    video_vars = {
        "prompt": "1.inputs.text",
        "seed": "2.inputs.seed",
        "width": "3.inputs.width",
        "height": "3.inputs.height",
        "seconds": "4.inputs.seconds",
        "steps": "2.inputs.steps",
    }
    if include_fps:
        video_vars["fps"] = "4.inputs.fps"
    return recipes.register_recipe(
        "0x" + "d" * 64,
        "video-contract-test",
        {
            "_grid": {
                "engine": "comfyui",
                "modelName": "Video Contract",
                "jobType": "video",
                "deterministic": False,
                "requiredModels": ["video-checkpoint"],
                "vars": video_vars,
                "clamps": {
                    "width": [512, 1024],
                    "height": [512, 1024],
                    "seconds": [1, 4],
                    "fps": [8, 24],
                    "steps": [4, 20],
                },
            },
            "1": {"inputs": {"text": ""}},
            "2": {"inputs": {"seed": 0, "steps": 8}},
            "3": {"inputs": {"width": 512, "height": 512}},
            "4": {"inputs": {"seconds": 2, "fps": 8}},
        },
        recipe_id=recipe_id,
    )


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
    assert preview["items"][0]["quality_eligible"] is False
    assert preview["items"][0]["quality_score"] is None


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
    challenge_prompts = set()
    expected_hashes = set()
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
        assert assignment["quality_eligible"] is False
        challenge_prompts.add(assignment["challenge"]["prompt"])
        expected_hashes.add(assignment["challenge"]["expected_hash"])
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
    assert len(challenge_prompts) == 3
    assert len(expected_hashes) == 3
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
async def test_open_v7_group_drains_with_its_shared_challenge(db):
    worker_id = str(uuid.uuid4())
    active_workers = [{
        "worker_id": worker_id,
        "name": "rig-v7-rollout",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
    }]
    first_account = uuid.uuid4()
    first_validator = await _register(first_account)
    first = (
        await validators_svc.issue_assignments(
            account_id=first_account,
            validator_id=first_validator,
            validator_wallet=TEST_WALLET,
            active_workers=active_workers,
            limit=1,
        )
    )["assignments"][0]
    shared_challenge = first["challenge"]

    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == first["probe_group_id"])
            .values(
                scoring_policy_id="text.generated.v7",
                challenge=shared_challenge,
                challenge_hash=validators_svc._hash_obj(
                    {
                        "group_id": first["probe_group_id"],
                        "worker_id": worker_id,
                        "model": first["model"],
                        "challenge": shared_challenge,
                    },
                ),
            ),
        )
        await session.commit()

    second_key = "0x" + f"{41:064x}"
    second_wallet = Account.from_key(second_key).address.lower()
    second_account = uuid.uuid4()
    second_validator = await _register(second_account, second_key)
    second = (
        await validators_svc.issue_assignments(
            account_id=second_account,
            validator_id=second_validator,
            validator_wallet=second_wallet,
            active_workers=active_workers,
            limit=1,
        )
    )["assignments"][0]

    assert second["probe_group_id"] == first["probe_group_id"]
    assert second["scoring_policy_id"] == "text.generated.v7"
    assert second["challenge"] == shared_challenge
    async with await database.new_session() as session:
        stored = await session.scalar(
            sa.select(assignments_t.c.challenge).where(
                assignments_t.c.id == second["assignment_id"],
            ),
        )
    assert stored == {}


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

    # The per-worker cadence is deliberate. Age the first capability group so
    # this test can exercise the separate legacy-capability group.
    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == rich_assignment["probe_group_id"])
            .values(
                created=validators_svc._now()
                - timedelta(
                    seconds=validators_svc.get_settings().validator_text_group_min_interval_seconds
                    + 1
                )
            )
        )
        await session.commit()

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
async def test_16k_context_assignment_requires_target_worker_headroom(db):
    private_key = "0x" + f"{32:064x}"
    wallet = Account.from_key(private_key).address.lower()
    account_id = uuid.uuid4()
    validator_id = await _register(
        account_id,
        private_key,
        capabilities=["text.context.16k.v1"],
    )
    worker = {
        "worker_id": str(uuid.uuid4()),
        "name": "rig-context",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
        "max_context_length": 16_384,
    }

    blocked = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert blocked["count"] == 0

    worker["max_context_length"] = 32_768
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert issued["count"] == 1
    assert issued["assignments"][0]["canary_kind"] == "context.retrieve.16k"
    assert issued["assignments"][0]["scoring_policy_id"] == "text.generated.v8"


@pytest.mark.asyncio
async def test_32k_context_assignment_requires_target_worker_headroom(db):
    private_key = "0x" + f"{33:064x}"
    wallet = Account.from_key(private_key).address.lower()
    account_id = uuid.uuid4()
    validator_id = await _register(
        account_id,
        private_key,
        capabilities=["text.context.32k.v1"],
    )
    worker = {
        "worker_id": str(uuid.uuid4()),
        "name": "rig-context-32k",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
        "max_context_length": 32_768,
    }

    blocked = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert blocked["count"] == 0

    worker["max_context_length"] = 65_536
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert issued["count"] == 1
    assert issued["assignments"][0]["canary_kind"] == "context.retrieve.32k"
    assert issued["assignments"][0]["scoring_policy_id"] == "text.generated.v8"

    assignment = issued["assignments"][0]
    async with await database.new_session() as session:
        stored_assignment = (
            await session.execute(
                sa.select(assignments_t.c.challenge).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).scalar_one()
        stored_group = (
            await session.execute(
                sa.select(probe_groups_t.c.challenge).where(
                    probe_groups_t.c.id == assignment["probe_group_id"]
                )
            )
        ).scalar_one()
    assert {
        key: stored_assignment[key]
        for key in assignment["challenge"]
    } == assignment["challenge"]
    assert stored_assignment["capability"] == assignment["capability"]
    assert stored_group == {
        "schema": "aipg.validator.text.batch.v1",
        "generator_kind": "context.retrieve.32k",
        "capability": "text.context.32k.v1",
        "score_dimension": "capability",
        "quality_eligible": False,
    }

    reloaded = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert reloaded["assignments"][0]["challenge"] == assignment["challenge"]


@pytest.mark.asyncio
async def test_text_group_cadence_blocks_immediate_replacement(db):
    private_key = "0x" + f"{34:064x}"
    wallet = Account.from_key(private_key).address.lower()
    account_id = uuid.uuid4()
    validator_id = await _register(
        account_id,
        private_key,
        capabilities=["text.context.32k.v1"],
    )
    worker = {
        "worker_id": str(uuid.uuid4()),
        "name": "rig-context-cadence",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
        "max_context_length": 65_536,
    }
    first = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assignment = first["assignments"][0]
    now = validators_svc._now()
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(status="finalized", quorum_status="finalized", finalized=now)
        )
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == assignment["probe_group_id"])
            .values(status="finalized", quorum_status="finalized", finalized=now)
        )
        await session.commit()

    blocked = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert blocked["count"] == 0

    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == assignment["probe_group_id"])
            .values(
                created=now
                - timedelta(
                    seconds=validators_svc.get_settings().validator_text_group_min_interval_seconds
                    + 1
                )
            )
        )
        await session.commit()
    replacement = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=[worker],
        limit=1,
    )
    assert replacement["count"] == 1


@pytest.mark.asyncio
async def test_prune_removes_finalized_machinery_but_keeps_signed_evidence(db):
    account_id = uuid.uuid4()
    validator_id, assignment, payload = await _assignment(account_id)
    await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=payload,
        signature=_sign(payload),
    )
    old = validators_svc._now() - timedelta(days=2)
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(status="finalized", quorum_status="finalized", finalized=old)
        )
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == assignment["probe_group_id"])
            .values(status="finalized", quorum_status="finalized", finalized=old)
        )
        await session.commit()

    deleted = await validators_svc.prune_validator_operational_history(
        older_than_days=1
    )
    assert deleted == {"assignments": 1, "probe_groups": 1}
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 0
        evidence = (
            await session.execute(sa.select(attestations_t.c.payload))
        ).scalar_one()
    assert evidence["evidence_hash"] == payload["evidence_hash"]


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
async def test_completed_probe_result_is_durable_and_replayable(db):
    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)
    row, job_id = await validators_svc._claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )
    result = {
        "status": "completed",
        "assignment_id": assignment["assignment_id"],
        "job_id": job_id,
        "grid_nonce": row["grid_nonce"],
        "probe_latency_ms": 4321,
        "evidence_hash": "a" * 64,
        "economic_effect": "none",
    }

    assert await validators_svc._mark_probe(
        job_id,
        "completed",
        evidence_hash=result["evidence_hash"],
        verdict="healthy",
        latency_ms=result["probe_latency_ms"],
        result=result,
    )
    pending = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[],
        limit=5,
    )
    assert [item["assignment_id"] for item in pending["assignments"]] == [
        assignment["assignment_id"]
    ]
    replayed = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert replayed == {**result, "replayed": True}
    async with await database.new_session() as session:
        stored = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
    assert stored["probe_attempts"] == 1
    assert stored["probe_result"] == result

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
        evidence_hash=result["evidence_hash"],
        verdict="healthy",
    )
    await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=payload,
        signature=_sign(payload),
    )
    delivered = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[],
        limit=5,
    )
    assert delivered["assignments"] == []
    with pytest.raises(validators_svc.AssignmentError, match="already submitted"):
        await validators_svc.probe_assignment(
            account_id=account_id,
            validator_id=validator_id,
            assignment_id=assignment["assignment_id"],
        )


@pytest.mark.asyncio
async def test_completed_probe_replay_is_owner_bound_and_grace_bounded(db):
    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)
    _, job_id = await validators_svc._claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )
    result = {
        "status": "completed",
        "assignment_id": assignment["assignment_id"],
        "job_id": job_id,
    }
    assert await validators_svc._mark_probe(job_id, "completed", result=result)

    with pytest.raises(validators_svc.AssignmentError, match="not found"):
        await validators_svc.probe_assignment(
            account_id=uuid.uuid4(),
            validator_id=validator_id,
            assignment_id=assignment["assignment_id"],
        )

    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(expires=validators_svc._now() - timedelta(seconds=1))
        )
        await session.commit()
    replayed = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )
    assert replayed["replayed"] is True

    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment["assignment_id"])
            .values(
                expires=validators_svc._now()
                - timedelta(seconds=validators_svc.ATTESTATION_GRACE_SECONDS + 1)
            )
        )
        await session.commit()
    with pytest.raises(validators_svc.AssignmentError, match="expired"):
        await validators_svc.probe_assignment(
            account_id=account_id,
            validator_id=validator_id,
            assignment_id=assignment["assignment_id"],
        )


@pytest.mark.asyncio
async def test_completed_probe_requires_bounded_replay_payload(db):
    account_id = uuid.uuid4()
    validator_id, assignment = await _fresh_assignment(account_id)
    _, job_id = await validators_svc._claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert not await validators_svc._mark_probe(job_id, "completed")
    with pytest.raises(validators_svc.AssignmentError, match="replay limit"):
        await validators_svc._mark_probe(
            job_id,
            "completed",
            result={
                "status": "completed",
                "output_text": "x" * validators_svc.MAX_PROBE_RESULT_BYTES,
            },
        )
    with pytest.raises(validators_svc.AssignmentError, match="not JSON serializable"):
        await validators_svc._mark_probe(
            job_id,
            "completed",
            result={"status": "completed", "bad": {object()}},
        )

    async with await database.new_session() as session:
        stored = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
    assert stored["probe_status"] == "running"
    assert stored["probe_result"] is None


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


@pytest.mark.asyncio
async def test_code_probe_forwards_only_prompt_and_scores_hidden_tests(db, monkeypatch):
    from grid_api.services import job_queue, token_stream

    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["text.code.v1"])
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": str(uuid.uuid4()), "name": "rig-code",
            "models": ["qwen3-27b"], "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    match = re.search(
        r"multiply x by (\d+), add (-?\d+), take the result modulo (\d+).*subtract (\d+)",
        challenge["prompt"],
    )
    assert match is not None
    multiplier, offset, modulus, adjustment = map(int, match.groups())
    output_text = (
        f"def {challenge['function_name']}(x):\n"
        f"    return ((x * {multiplier} + {offset}) % {modulus}) - {adjustment}"
    )
    submitted = {}

    async def capture_submit(job_id, payload, models, **kwargs):
        submitted.update(job_id=job_id, payload=payload, models=models, kwargs=kwargs)

    async def completed_events(*_args, **_kwargs):
        yield {
            "text": token_stream.DONE_SENTINEL,
            "full_text": output_text,
            "finish_reason": "stop",
            "usage": {"completion_tokens": 32},
            "grid": {"worker": "rig-code"},
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert result["canary_kind"] == "code.function"
    assert submitted["payload"]["request"]["messages"][-1]["content"] == challenge["prompt"]
    assert "test_inputs" not in json.dumps(submitted["payload"])
    assert submitted["kwargs"]["hard_target_worker"] == "rig-code"
    async with await database.new_session() as session:
        row = (await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment["assignment_id"])
        )).mappings().one()
    assert row["probe_status"] == "completed"
    assert row["probe_verdict"] == "healthy"


@pytest.mark.asyncio
async def test_token_limit_probe_forwards_budget_and_commits_terminal_evidence(db, monkeypatch):
    from grid_api.services import den, job_queue, token_stream

    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["text.token_limit.v1"])
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=[{
            "worker_id": str(uuid.uuid4()), "name": "rig-token-limit",
            "models": ["qwen3-27b"], "job_types": ["text"],
        }],
        limit=1,
    )
    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    token = challenge["prompt"].split("Repeat exactly ", 1)[1].split(" ", 1)[0]
    pieces = []
    while den.count_tokens(" ".join(pieces)) < challenge["max_tokens"] // 2:
        pieces.append(token)
    output_text = " ".join(pieces)
    submitted = {}

    async def capture_submit(job_id, payload, models, **kwargs):
        submitted.update(job_id=job_id, payload=payload, models=models, kwargs=kwargs)

    async def completed_events(*_args, **_kwargs):
        yield {
            "text": token_stream.DONE_SENTINEL,
            "full_text": output_text,
            "full_reasoning": "",
            "finish_reason": "length",
            "usage": {"completion_tokens": 1},
            "grid": {"worker": "rig-token-limit"},
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert result["canary_kind"] == "token.limit"
    assert result["finish_reason"] == "length"
    assert submitted["payload"]["request"]["max_tokens"] == challenge["max_tokens"]
    assert submitted["kwargs"]["hard_target_worker"] == "rig-token-limit"
    response_commitment = validators_svc._canonical({
        "text": output_text,
        "reasoning": "",
        "finish_reason": "length",
    })
    assert result["response_hash"] == validators_svc._hash_text(response_commitment)

    async with await database.new_session() as session:
        row = (await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment["assignment_id"])
        )).mappings().one()
    assert row["probe_status"] == "completed"
    assert row["probe_verdict"] == "healthy"
    assert row["probe_response_hash"] == result["response_hash"]


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
    assert body["features"]["score_dimensions"] is True
    assert body["features"]["unique_text_batch_challenges"] is True
    assert body["features"]["blind_quality"] is False
    assert body["features"]["worker_terminal_indistinguishable"] is False
    assert body["probe_policy"]["max_attempts"] >= 1
    assert body["probe_policy"]["lease_seconds"] > validators_svc.PROBE_TIMEOUT_SECONDS
    assert body["probe_policy"]["text_batch_scoring_policy"] == "text.generated.v8"
    assert body["probe_policy"]["challenge_instance"] == "unique_per_validator"
    assert body["probe_policy"]["quality_eligible"] is False
    assert body["probe_policy"]["worker_payload_hides_assignment"] is True
    assert body["probe_policy"]["worker_terminal_indistinguishable"] is False
    assert (
        body["authority_model"]["authoritative"]
        == "requires Grid-issued assignment_id + grid_nonce + probe evidence hash"
    )
    assert body["quorum_policy"]["threshold"] == 3
    assert body["quorum_policy"]["target_validators"] == 5
    assert body["quorum_policy"]["operator_independence_proven"] is False
    assert body["features"]["image_fidelity"] is False
    assert body["features"]["video_validation"] is False
    assert body["media_validation"]["image"]["economic_effect"] == "none"
    assert body["media_validation"]["video"]["economic_effect"] == "none"


@pytest.mark.asyncio
async def test_image_assignment_gate_is_fail_closed(db, monkeypatch):
    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["image.fidelity.v1"])
    monkeypatch.setattr(validators_svc, "get_settings", lambda: _media_settings(enabled=False))

    with pytest.raises(validators_svc.AssignmentError, match="not enabled"):
        await validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=TEST_WALLET,
            active_workers=[],
            limit=1,
            modality="image",
        )


@pytest.mark.asyncio
async def test_image_assignment_requires_governed_recipe_and_bonded_references(db, monkeypatch):
    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["image.fidelity.v1"])
    monkeypatch.setattr(validators_svc, "get_settings", lambda: _media_settings())
    now = datetime.now(UTC)
    async with await database.new_session() as session:
        candidate = await _seed_image_worker(session, 11, now=now)
        refs = [
            await _seed_image_worker(session, 12, now=now),
            await _seed_image_worker(session, 13, now=now),
        ]
        for reference in refs:
            await _seed_image_reference(session, reference, now=now)
        await session.commit()
    active = [
        {
            "worker_id": str(worker[0]),
            "name": f"image-rig-{index}",
            "models": ["deterministic-checkpoint"],
            "job_types": ["image"],
        }
        for worker, index in zip([candidate, *refs], (11, 12, 13), strict=True)
    ]

    _register_image_recipe(deterministic=False)
    blocked = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=active,
        limit=1,
        modality="image",
    )
    assert blocked["assignments"] == []

    _register_image_recipe()
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=active,
        limit=1,
        modality="image",
    )

    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    assert assignment["target_worker_id"] == str(candidate[0])
    assert assignment["capability"] == "image.fidelity.v1"
    assert challenge["schema"] == "aipg.validator.media.challenge.v1"
    assert challenge["recipe_id"] == 42
    assert challenge["model_digest"] == "c" * 64
    assert challenge["parameters"]["width"] == 512
    assert challenge["parameters"]["height"] == 512
    assert set(challenge["reference_worker_ids"]) == {str(refs[0][0]), str(refs[1][0])}
    assert issued["economic_effect"] == "none"

    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == assignment["probe_group_id"])
            .values(
                probe_job_id="abandoned-image-probe",
                probe_status="running",
                probe_attempts=1,
                probe_lease_expires=now - timedelta(seconds=1),
            )
        )
        await session.commit()

    from grid_api.services import job_queue, token_stream

    worker_ids_by_name = {
        f"image-rig-{index}": str(worker[0])
        for worker, index in zip([candidate, *refs], (11, 12, 13), strict=True)
    }
    submitted = {}

    async def capture_submit(stage_job_id, payload, models, **kwargs):
        submitted[stage_job_id] = {"payload": payload, "models": models, **kwargs}

    async def completed_events(stage_job_id, **_kwargs):
        item = submitted[stage_job_id]
        payload = item["payload"]
        worker_id = worker_ids_by_name[item["hard_target_worker"]]
        witness = {
            "role": payload["_validator_role"],
            "worker_id": worker_id,
            "url": f"https://media.example/validator/{stage_job_id}/0.webp",
            "sha256": hashlib.sha256(worker_id.encode()).hexdigest(),
            "bytes": 123,
            "content_type": "image/webp",
            "latency_ms": 100,
        }
        yield {
            "text": token_stream.DONE_SENTINEL,
            "full_text": json.dumps({"witness": witness}),
            "grid": {
                "worker_id": worker_id,
                "assignment_id": assignment["assignment_id"],
                "grid_nonce": assignment["grid_nonce"],
                "economic_effect": "none",
            },
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert [item["role"] for item in result["witnesses"]] == [
        "candidate", "reference", "reference",
    ]
    assert {item["worker_id"] for item in result["witnesses"]} == {
        str(candidate[0]), str(refs[0][0]), str(refs[1][0]),
    }
    assert len(submitted) == 3
    assert {item["hard_target_worker"] for item in submitted.values()} == {
        "image-rig-11", "image-rig-12", "image-rig-13",
    }
    assert all(item["models"] == ["deterministic-checkpoint"] for item in submitted.values())
    assert result["prompt_hash"] == validators_svc._hash_text(
        validators_svc._canonical(challenge)
    )
    async with await database.new_session() as session:
        stored = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.id == assignment["assignment_id"]
                )
            )
        ).mappings().one()
        stored_group = (
            await session.execute(
                sa.select(probe_groups_t).where(
                    probe_groups_t.c.id == assignment["probe_group_id"]
                )
            )
        ).mappings().one()
    assert stored["probe_status"] == "completed"
    assert stored["probe_verdict"] == "witnessed"
    assert stored_group["probe_status"] == "completed"
    assert stored_group["probe_attempts"] == 2
    assert stored_group["probe_witness_hash"] == validators_svc._hash_obj(
        {"witnesses": result["witnesses"]}
    )

    second_key = "0x" + "02" * 32
    second_account = uuid.uuid4()
    second_validator = await _register(
        second_account,
        private_key=second_key,
        capabilities=["image.fidelity.v1"],
    )
    second_wallet = Account.from_key(second_key).address.lower()
    second_issued = await validators_svc.issue_assignments(
        account_id=second_account,
        validator_id=second_validator,
        validator_wallet=second_wallet,
        active_workers=active,
        limit=1,
        modality="image",
    )
    second_assignment = second_issued["assignments"][0]
    assert second_assignment["probe_group_id"] == assignment["probe_group_id"]
    reused = await validators_svc.probe_assignment(
        account_id=second_account,
        validator_id=second_validator,
        assignment_id=second_assignment["assignment_id"],
    )
    assert reused["status"] == "completed"
    assert reused["witnesses"] == result["witnesses"]
    assert len(submitted) == 3

    attestation = _payload(
        assignment_source="grid",
        assignment_id=assignment["assignment_id"],
        probe_group_id=assignment["probe_group_id"],
        grid_nonce=assignment["grid_nonce"],
        worker_id=assignment["target_worker_id"],
        model=assignment["model"],
        modality="image",
        capability="image.fidelity.v1",
        canary_kind="image.fidelity",
        evidence_hash=result["evidence_hash"],
        verdict="healthy",
    )
    accepted = await validators_svc.record_attestation(
        account_id=account_id,
        validator_id=validator_id,
        payload=attestation,
        signature=_sign(attestation),
    )
    assert accepted["authority"] == "authoritative"


@pytest.mark.asyncio
async def test_video_assignment_gate_is_fail_closed(db, monkeypatch):
    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["video.contract.v1"])
    monkeypatch.setattr(
        validators_svc,
        "get_settings",
        lambda: _media_settings(enabled=True, video_enabled=False),
    )

    with pytest.raises(validators_svc.AssignmentError, match="not enabled"):
        await validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=TEST_WALLET,
            active_workers=[],
            limit=1,
            modality="video",
        )


@pytest.mark.asyncio
async def test_video_contract_assignment_is_governed_targeted_and_shared(db, monkeypatch):
    account_id = uuid.uuid4()
    validator_id = await _register(account_id, capabilities=["video.contract.v1"])
    monkeypatch.setattr(
        validators_svc,
        "get_settings",
        lambda: _media_settings(enabled=True, video_enabled=True),
    )
    worker_id = str(uuid.uuid4())
    active = [{
        "worker_id": worker_id,
        "name": "video-rig-1",
        "models": ["video-checkpoint"],
        "job_types": ["video"],
    }]

    _register_video_recipe(include_fps=False)
    blocked = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=active,
        limit=1,
        modality="video",
    )
    assert blocked["assignments"] == []

    _register_video_recipe()
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=TEST_WALLET,
        active_workers=active,
        limit=1,
        modality="video",
    )
    assignment = issued["assignments"][0]
    challenge = assignment["challenge"]
    assert assignment["target_worker_id"] == worker_id
    assert assignment["capability"] == "video.contract.v1"
    assert challenge["kind"] == "video.contract"
    assert challenge["recipe_id"] == 84
    assert challenge["reference_worker_ids"] == []
    assert challenge["parameters"] == {
        "seed": challenge["seed"],
        "width": 512,
        "height": 512,
        "frame_count": 16,
        "fps": 8,
        "duration_s": 2.0,
        "motion_required": True,
        "steps": 8,
    }
    assert issued["economic_effect"] == "none"

    from grid_api.services import job_queue, token_stream

    submitted = {}

    async def capture_submit(stage_job_id, payload, models, **kwargs):
        submitted[stage_job_id] = {"payload": payload, "models": models, **kwargs}

    async def completed_events(stage_job_id, **_kwargs):
        witness = {
            "role": "candidate",
            "worker_id": worker_id,
            "url": f"https://media.example/validator/{stage_job_id}/0.mp4",
            "sha256": "e" * 64,
            "bytes": 456,
            "content_type": "video/mp4",
            "latency_ms": 900,
        }
        yield {
            "text": token_stream.DONE_SENTINEL,
            "full_text": json.dumps({"witness": witness}),
            "grid": {
                "worker_id": worker_id,
                "assignment_id": assignment["assignment_id"],
                "grid_nonce": assignment["grid_nonce"],
                "economic_effect": "none",
            },
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    result = await validators_svc.probe_assignment(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment["assignment_id"],
    )

    assert result["status"] == "completed"
    assert result["witnesses"][0]["content_type"] == "video/mp4"
    assert result["witnesses"][0]["worker_id"] == worker_id
    assert len(submitted) == 1
    dispatched = next(iter(submitted.values()))
    assert dispatched["job_type"] == "video"
    assert dispatched["hard_target_worker"] == "video-rig-1"
    assert dispatched["models"] == ["video-checkpoint"]
    assert dispatched["payload"]["frames"] == 16
    assert dispatched["payload"]["fps"] == 8

    second_key = "0x" + "03" * 32
    second_account = uuid.uuid4()
    second_validator = await _register(
        second_account,
        private_key=second_key,
        capabilities=["video.contract.v1"],
    )
    second_wallet = Account.from_key(second_key).address.lower()
    second_issued = await validators_svc.issue_assignments(
        account_id=second_account,
        validator_id=second_validator,
        validator_wallet=second_wallet,
        active_workers=active,
        limit=1,
        modality="video",
    )
    second_assignment = second_issued["assignments"][0]
    assert second_assignment["probe_group_id"] == assignment["probe_group_id"]
    reused = await validators_svc.probe_assignment(
        account_id=second_account,
        validator_id=second_validator,
        assignment_id=second_assignment["assignment_id"],
    )
    assert reused["status"] == "completed"
    assert reused["witnesses"] == result["witnesses"]
    assert len(submitted) == 1


def test_media_group_witness_commitment_fails_closed(monkeypatch):
    monkeypatch.setattr(validators_svc, "get_settings", lambda: _media_settings())
    candidate = str(uuid.uuid4())
    references = [str(uuid.uuid4()), str(uuid.uuid4())]
    challenge = {"reference_worker_ids": references, "seed": 7}
    row = {
        "probe_group_id": "prg-test",
        "target_worker_id": candidate,
        "model": "deterministic-checkpoint",
        "modality": "image",
        "capability": "image.fidelity.v1",
        "canary_kind": "image.fidelity",
        "challenge": challenge,
    }
    witnesses = [
        {
            "role": role,
            "worker_id": worker_id,
            "url": f"https://media.example/validator/{index}.webp",
            "sha256": f"{index + 1}" * 64,
            "bytes": 123,
            "content_type": "image/webp",
            "latency_ms": 100,
        }
        for index, (role, worker_id) in enumerate(
            [("candidate", candidate), ("reference", references[0]), ("reference", references[1])]
        )
    ]
    group = {
        "id": row["probe_group_id"],
        "target_worker_id": candidate,
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "challenge": challenge,
        "challenge_hash": validators_svc._hash_obj({
            "group_id": row["probe_group_id"],
            "worker_id": candidate,
            "model": row["model"],
            "challenge": challenge,
        }),
        "probe_witnesses": witnesses,
        "probe_witness_hash": validators_svc._hash_obj({"witnesses": witnesses}),
    }
    validators_svc._verify_media_group_binding(row, group)
    assert validators_svc._verified_media_group_witnesses(row, group) == witnesses

    group["probe_witnesses"][0]["url"] = "https://media.example/tampered.webp"
    with pytest.raises(validators_svc.AssignmentError, match="commitment"):
        validators_svc._verified_media_group_witnesses(row, group)

    group["challenge"] = {**challenge, "seed": 8}
    with pytest.raises(validators_svc.AssignmentError, match="does not match"):
        validators_svc._verify_media_group_binding(row, group)


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
