# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres proofs for shared validator-group allocation."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import auth, database, safe_logging
from grid_api.services import validator_references
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import validator_assignments as assignments_t
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


async def _seed_validators(count: int):
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
                    capabilities=["text.basic.v1"],
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
