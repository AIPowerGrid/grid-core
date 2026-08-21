# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres proofs for shared validator-group allocation."""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import auth, database, safe_logging
from grid_api.services import validators as validators_svc
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import validator_assignments as assignments_t
from grid_api.v2.schema import validator_probe_groups as probe_groups_t
from grid_api.v2.schema import validators as validators_t


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
                sa.insert(accounts_t).values(id=account_id, wallet=wallet, flags={})
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
                )
            )
            rows.append((account_id, validator_id, wallet, private_key))
        await session.commit()
    return rows


def _workers():
    return [{
        "worker_id": str(uuid.uuid4()),
        "name": "pg-quorum-rig",
        "models": ["qwen3-27b"],
        "job_types": ["text"],
    }]


@pytest.mark.asyncio
async def test_concurrent_validators_join_one_shared_probe_group(pg):
    validators = await _seed_validators(5)
    workers = _workers()
    results = await asyncio.gather(*[
        validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=wallet,
            active_workers=workers,
            limit=1,
        )
        for account_id, validator_id, wallet, _private_key in validators
    ])

    group_ids = {
        result["assignments"][0]["probe_group_id"]
        for result in results
    }
    assert len(group_ids) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 5


@pytest.mark.asyncio
async def test_concurrent_polls_from_one_validator_reuse_one_assignment(pg):
    account_id, validator_id, wallet, _private_key = (await _seed_validators(1))[0]
    workers = _workers()
    results = await asyncio.gather(*[
        validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=wallet,
            active_workers=workers,
            limit=1,
        )
        for _ in range(8)
    ])

    assignment_ids = {
        result["assignments"][0]["assignment_id"]
        for result in results
    }
    assert len(assignment_ids) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(probe_groups_t)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(assignments_t)) == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_votes_reach_quorum_atomically(pg):
    validators = await _seed_validators(3)
    workers = _workers()
    issued = await asyncio.gather(*[
        validators_svc.issue_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=wallet,
            active_workers=workers,
            limit=1,
        )
        for account_id, validator_id, wallet, _private_key in validators
    ])
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
                )
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

    await asyncio.gather(*[
        submit(index, validator)
        for index, validator in enumerate(validators)
    ])

    group_id = assignments[0]["probe_group_id"]
    async with await database.new_session() as session:
        group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == group_id)
            )
        ).mappings().one()
    assert group["quorum_status"] == "accepted"
    assert group["quorum_outcome"] == "healthy"
