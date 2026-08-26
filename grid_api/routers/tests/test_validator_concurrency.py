# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres proofs for shared validator-group allocation."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import auth, database, safe_logging
from grid_api.services import recipes, validator_references
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import validator_assignments as assignments_t
from grid_api.v2.schema import validator_attestations as attestations_t
from grid_api.v2.schema import validator_probe_groups as probe_groups_t
from grid_api.v2.schema import validator_reference_workers as references_t
from grid_api.v2.schema import validators as validators_t
from grid_api.v2.schema import workers as workers_t

_PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not _PG.startswith("postgresql"),
    reason="set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database",
)


@pytest.fixture(autouse=True)
def _isolated_logging_salt(monkeypatch):
    monkeypatch.setenv("GRID_SALT", "validator-concurrency-test-only-salt")
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    safe_logging._log_key.cache_clear()
    yield
    safe_logging._log_key.cache_clear()


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(_PG)
    async with engine.begin() as connection:
        await connection.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield
    finally:
        database._session_factory = old
        async with engine.begin() as connection:
            await connection.run_sync(v2_metadata.drop_all)
        await engine.dispose()


async def _seed_validators(count: int, *, capabilities=None):
    rows = []
    async with await database.new_session() as session:
        for index in range(count):
            account_id = uuid.uuid4()
            validator_id = f"val_pg_{index}_{uuid.uuid4().hex}"
            private_key = "0x" + f"{index + 21:064x}"
            wallet = Account.from_key(private_key).address.lower()
            await session.execute(
                sa.insert(accounts_t).values(id=account_id, wallet=wallet, flags={}),
            )
            await session.execute(
                sa.insert(validators_t).values(
                    id=validator_id,
                    account_id=account_id,
                    signing_wallet=wallet,
                    software_version="pg-test",
                    capabilities=capabilities or ["text.basic.v1"],
                    registration_signature="0x" + "11" * 65,
                    status="active",
                    last_heartbeat=validators_svc._now(),
                    created=validators_svc._now(),
                    updated=validators_svc._now(),
                ),
            )
            rows.append((account_id, validator_id, wallet, private_key))
        await session.commit()
    return rows


def _workers():
    return [
        {
            "worker_id": str(uuid.uuid4()),
            "name": "pg-quorum-rig",
            "models": ["qwen3-27b"],
            "job_types": ["text"],
        },
    ]


async def _seed_media_worker(session, index, *, now):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    wallet = f"0x{index:040x}"
    await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
    await session.execute(
        sa.insert(workers_t).values(
            id=worker_id,
            account_id=account_id,
            name=f"pg-media-rig-{index}-{worker_id.hex[:8]}",
            type="image",
            wallet=wallet,
            models=["krea-2-turbo"],
            capabilities={},
            maintenance=False,
            first_seen=now - timedelta(days=1),
            last_seen=now,
            jobs_completed=100,
            den_earned=0,
        ),
    )
    return worker_id, account_id, wallet


async def _seed_reference(session, worker, *, now, bond_amount_raw):
    worker_id, account_id, wallet = worker
    await session.execute(
        sa.insert(references_t).values(
            worker_id=worker_id,
            model="krea-2-turbo",
            modality="image",
            account_id=account_id,
            payout_wallet=wallet,
            status="active",
            status_reason="postgres test fixture",
            bond_contract="0x" + "a" * 40,
            bond_chain_id=8453,
            bond_finalized_block=123456,
            bond_amount_raw=Decimal(bond_amount_raw),
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
        ),
    )


def _reference_policy():
    return {
        "model": "krea-2-turbo",
        "modality": "image",
        "expected_chain_id": 8453,
        "expected_bond_contract": "0x" + "a" * 40,
        "expected_verifier_version": "worker-registry-v2",
        "minimum_bond_raw": 10**18,
        "minimum_quality_pass_rate": 0.95,
    }


def _media_settings():
    return SimpleNamespace(
        validator_media_probe_enabled=True,
        validator_media_bond_sync_enabled=True,
        base_rpc_url=SecretStr("https://rpc.invalid"),
        validator_media_bond_chain_id=8453,
        validator_media_bond_contract="0x" + "a" * 40,
        validator_media_bond_facet_runtime_hash="0x" + "b" * 64,
        validator_media_bond_confirmation_rpc_url=SecretStr(
            "https://rpc-two.invalid",
        ),
        validator_media_bond_verifier_version="worker-registry-v2",
        validator_media_minimum_bond_raw=10**18,
        validator_media_minimum_quality_pass_rate=0.95,
        validator_media_max_output_bytes=25 * 1024 * 1024,
        validator_media_probe_timeout_seconds=600,
    )


def _seed_deterministic_image_recipe():
    recipes._BY_ROOT.clear()
    recipes._BY_ID.clear()
    recipes._BY_NAME.clear()
    recipes._BY_MODEL.clear()
    recipes.register_recipe(
        "0x" + "b" * 64,
        "postgres-image-fidelity",
        {
            "_grid": {
                "modelName": "krea-2-turbo",
                "jobType": "image",
                "deterministic": True,
                "modelDigest": "c" * 64,
                "requiredModels": ["krea-2-turbo"],
                "vars": {
                    "prompt": "1.inputs.text",
                    "seed": "2.inputs.seed",
                    "width": "3.inputs.width",
                    "height": "3.inputs.height",
                },
            },
            "1": {"inputs": {"text": ""}},
            "2": {"inputs": {"seed": 0}},
            "3": {"inputs": {"width": 512, "height": 512}},
        },
        recipe_id=77,
    )


@pytest.mark.asyncio
async def test_reference_bond_threshold_is_exact_at_uint_scale_on_postgres(pg):
    now = datetime.now(UTC)
    async with await database.new_session() as session:
        candidate = await _seed_media_worker(session, 101, now=now)
        references = [await _seed_media_worker(session, index, now=now) for index in (102, 103, 104)]
        for reference, amount in zip(references, (10**18, 10**18 + 1, 10**18 - 1), strict=True):
            await _seed_reference(session, reference, now=now, bond_amount_raw=amount)
        await session.commit()

    async with await database.new_session() as session:
        selected = await validator_references.select_reference_workers(
            session,
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], *(item[0] for item in references)],
            now=now,
            **_reference_policy(),
        )
        await session.commit()

    assert {item.worker_id for item in selected} == {references[0][0], references[1][0]}


@pytest.mark.asyncio
async def test_concurrent_reference_groups_lock_only_their_selected_pair(pg):
    now = datetime.now(UTC)
    async with await database.new_session() as session:
        candidate = await _seed_media_worker(session, 201, now=now)
        references = [await _seed_media_worker(session, index, now=now) for index in range(202, 206)]
        for reference in references:
            await _seed_reference(session, reference, now=now, bond_amount_raw=10**18)
        await session.commit()

    online = [candidate[0], *(item[0] for item in references)]
    first_ready = asyncio.Event()
    release_first = asyncio.Event()

    async def select_first():
        async with await database.new_session() as session:
            selected = await validator_references.select_reference_workers(
                session,
                candidate_worker_id=candidate[0],
                online_model_worker_ids=online,
                now=now,
                **_reference_policy(),
            )
            first_ready.set()
            await release_first.wait()
            await session.commit()
            return selected

    first_task = asyncio.create_task(select_first())
    await asyncio.wait_for(first_ready.wait(), timeout=5)
    try:
        async with await database.new_session() as session:
            second = await validator_references.select_reference_workers(
                session,
                candidate_worker_id=candidate[0],
                online_model_worker_ids=online,
                now=now,
                **_reference_policy(),
            )
            await session.commit()
    finally:
        release_first.set()
    first = await asyncio.wait_for(first_task, timeout=5)

    assert {item.worker_id for item in first}.isdisjoint(
        {item.worker_id for item in second},
    )


@pytest.mark.asyncio
async def test_concurrent_image_validators_join_one_bonded_reference_group(pg, monkeypatch):
    validators = await _seed_validators(5, capabilities=["image.fidelity.v1"])
    monkeypatch.setattr(validators_svc, "get_settings", _media_settings)
    _seed_deterministic_image_recipe()
    now = datetime.now(UTC)
    async with await database.new_session() as session:
        candidate = await _seed_media_worker(session, 301, now=now)
        references = [
            await _seed_media_worker(session, 302, now=now),
            await _seed_media_worker(session, 303, now=now),
        ]
        for reference in references:
            await _seed_reference(session, reference, now=now, bond_amount_raw=10**18)
        await session.commit()
    workers = [
        {
            "worker_id": str(worker[0]),
            "name": f"pg-media-rig-{index}-{worker[0].hex[:8]}",
            "models": ["krea-2-turbo"],
            "job_types": ["image"],
        }
        for worker, index in zip([candidate, *references], (301, 302, 303), strict=True)
    ]

    results = await asyncio.gather(
        *[
            validators_svc.issue_assignments(
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                active_workers=workers,
                limit=1,
                modality="image",
            )
            for account_id, validator_id, wallet, _private_key in validators
        ],
    )

    group_ids = {result["assignments"][0]["probe_group_id"] for result in results}
    assert len(group_ids) == 1
    async with await database.new_session() as session:
        group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == next(iter(group_ids)))
            )
        ).mappings().one()
        assert await session.scalar(
            sa.select(sa.func.count()).select_from(assignments_t).where(
                assignments_t.c.probe_group_id == group["id"],
            )
        ) == 5
    assert set(group["challenge"]["reference_worker_ids"]) == {
        str(references[0][0]), str(references[1][0]),
    }

    from grid_api.services import job_queue, token_stream

    worker_ids_by_name = {
        f"pg-media-rig-{index}-{worker[0].hex[:8]}": str(worker[0])
        for worker, index in zip([candidate, *references], (301, 302, 303), strict=True)
    }
    submitted = {}
    all_stages_submitted = asyncio.Event()
    release_results = asyncio.Event()

    async def capture_submit(stage_job_id, payload, models, **kwargs):
        submitted[stage_job_id] = {"payload": payload, "models": models, **kwargs}
        if len(submitted) == 3:
            all_stages_submitted.set()

    async def completed_events(stage_job_id, **_kwargs):
        await release_results.wait()
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
                "assignment_id": payload["_validator_assignment_id"],
                "grid_nonce": payload["_validator_grid_nonce"],
                "economic_effect": "none",
            },
        }

    monkeypatch.setattr(job_queue, "submit_job", capture_submit)
    monkeypatch.setattr(token_stream, "subscribe_tokens", completed_events)
    tasks = [
        asyncio.create_task(
            validators_svc.probe_assignment(
                account_id=account_id,
                validator_id=validator_id,
                assignment_id=issued["assignments"][0]["assignment_id"],
            )
        )
        for (account_id, validator_id, _wallet, _private_key), issued in zip(
            validators,
            results,
            strict=True,
        )
    ]
    await asyncio.wait_for(all_stages_submitted.wait(), timeout=5)
    await asyncio.sleep(0.1)
    release_results.set()
    probed = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    assert len(submitted) == 3
    assert all(result["status"] == "completed" for result in probed)
    assert all(result["witnesses"] == probed[0]["witnesses"] for result in probed)
    async with await database.new_session() as session:
        final_group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == group["id"])
            )
        ).mappings().one()
        assignment_rows = (
            await session.execute(
                sa.select(assignments_t).where(assignments_t.c.probe_group_id == group["id"])
            )
        ).mappings().all()
    assert final_group["probe_status"] == "completed"
    assert final_group["probe_attempts"] == 1
    assert all(row["probe_status"] == "completed" for row in assignment_rows)
    assert all(row["probe_attempts"] == 1 for row in assignment_rows)


@pytest.mark.asyncio
async def test_concurrent_validators_join_one_shared_probe_group(pg):
    validators = await _seed_validators(5)
    workers = _workers()
    results = await asyncio.gather(
        *[
            validators_svc.issue_assignments(
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                active_workers=workers,
                limit=1,
            )
            for account_id, validator_id, wallet, _private_key in validators
        ],
    )

    group_ids = {result["assignments"][0]["probe_group_id"] for result in results}
    assert len(group_ids) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 5


@pytest.mark.asyncio
async def test_concurrent_common_control_gets_one_shared_probe_seat(pg):
    validators = await _seed_validators(5)
    async with await database.new_session() as session:
        for index, (_account_id, validator_id, _wallet, _key) in enumerate(validators):
            operator_group = "opg_common_control_01" if index < 2 else f"opg_independent_{index:02d}"
            await session.execute(
                sa.update(validators_t)
                .where(validators_t.c.id == validator_id)
                .values(operator_group_id=operator_group),
            )
        await session.commit()

    workers = _workers()
    results = await asyncio.gather(
        *[
            validators_svc.issue_assignments(
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                active_workers=workers,
                limit=1,
            )
            for account_id, validator_id, wallet, _private_key in validators
        ],
    )

    assert sum(bool(result["assignments"]) for result in results[:2]) == 1
    assert all(result["assignments"] for result in results[2:])
    assignments = [item for result in results for item in result["assignments"]]
    assert len(assignments) == 4
    assert len({item["probe_group_id"] for item in assignments}) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 4


@pytest.mark.asyncio
async def test_postgres_prune_preserves_evidence_and_clears_retired_links(pg):
    account_id, validator_id, wallet, _private_key = (await _seed_validators(1))[0]
    issued = await validators_svc.issue_assignments(
        account_id=account_id,
        validator_id=validator_id,
        validator_wallet=wallet,
        active_workers=_workers(),
        limit=1,
    )
    assignment = issued["assignments"][0]
    evidence_hash = "a" * 64
    evidence_payload = {
        "assignment_id": assignment["assignment_id"],
        "probe_group_id": assignment["probe_group_id"],
        "evidence_hash": evidence_hash,
    }
    old = validators_svc._now() - timedelta(days=2)
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(attestations_t).values(
                attestation_hash="b" * 64,
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                assignment_id=assignment["assignment_id"],
                probe_group_id=assignment["probe_group_id"],
                grid_nonce=assignment["grid_nonce"],
                evidence_hash=evidence_hash,
                authority="authoritative",
                quorum_status="finalized",
                worker_id=assignment["target_worker_id"],
                model=assignment["model"],
                modality="text",
                capability=assignment["capability"],
                canary_kind=assignment["canary_kind"],
                verdict="healthy",
                signature="0x" + "11" * 65,
                signature_status="verified",
                payload=evidence_payload,
                created=old,
            )
        )
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
        older_than_days=1,
    )
    assert deleted == {"assignments": 1, "probe_groups": 1}
    async with await database.new_session() as session:
        evidence = (
            await session.execute(
                sa.select(
                    attestations_t.c.assignment_id,
                    attestations_t.c.probe_group_id,
                    attestations_t.c.payload,
                )
            )
        ).mappings().one()
    assert evidence["assignment_id"] is None
    assert evidence["probe_group_id"] is None
    assert evidence["payload"] == evidence_payload


@pytest.mark.asyncio
async def test_concurrent_polls_from_one_validator_reuse_one_assignment(pg):
    account_id, validator_id, wallet, _private_key = (await _seed_validators(1))[0]
    workers = _workers()
    results = await asyncio.gather(
        *[
            validators_svc.issue_assignments(
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                active_workers=workers,
                limit=1,
            )
            for _ in range(8)
        ],
    )

    assignment_ids = {result["assignments"][0]["assignment_id"] for result in results}
    assert len(assignment_ids) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_votes_reach_quorum_atomically(pg):
    validators = await _seed_validators(3)
    workers = _workers()
    issued = await asyncio.gather(
        *[
            validators_svc.issue_assignments(
                account_id=account_id,
                validator_id=validator_id,
                validator_wallet=wallet,
                active_workers=workers,
                limit=1,
            )
            for account_id, validator_id, wallet, _private_key in validators
        ],
    )
    assignments = [result["assignments"][0] for result in issued]
    async with await database.new_session() as session:
        for index, assignment in enumerate(assignments):
            await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.id == assignment["assignment_id"])
                .values(
                    probe_status="completed",
                    probe_evidence_hash=f"{index + 1}" * 64,
                    probe_verdict="healthy",
                ),
            )
        await session.commit()

    async def submit(index, validator):
        account_id, validator_id, wallet, private_key = validator
        assignment = assignments[index]
        payload = {
            "validator": wallet,
            "attestation_schema": "aipg.validator.attestation.v0",
            "assignment_source": "grid",
            "assignment_id": assignment["assignment_id"],
            "probe_group_id": assignment["probe_group_id"],
            "grid_nonce": assignment["grid_nonce"],
            "worker_id": assignment["target_worker_id"],
            "model": assignment["model"],
            "modality": assignment["modality"],
            "capability": assignment["capability"],
            "canary_kind": assignment["canary_kind"],
            "evidence_hash": f"{index + 1}" * 64,
            "verdict": "healthy",
            "score": 1.0,
            "latency_ms": 10,
        }
        signature = Account.sign_message(
            encode_defunct(text=validators_svc._canonical(payload)),
            private_key=private_key,
        ).signature.hex()
        return await validators_svc.record_attestation(
            account_id=account_id,
            validator_id=validator_id,
            payload=payload,
            signature=signature,
        )

    await asyncio.gather(*[submit(index, validator) for index, validator in enumerate(validators)])

    group_id = assignments[0]["probe_group_id"]
    async with await database.new_session() as session:
        group = (
            (
                await session.execute(
                    sa.select(probe_groups_t).where(probe_groups_t.c.id == group_id),
                )
            )
            .mappings()
            .one()
        )
    assert group["quorum_status"] == "accepted"
    assert group["quorum_outcome"] == "healthy"
