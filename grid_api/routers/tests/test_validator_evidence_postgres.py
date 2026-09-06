# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adversarial signed-evidence checks on a disposable real PostgreSQL only.

Fixtures synthesize completed probes; this does not prove inference or HTTP auth.
"""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct

from grid_api import database
from grid_api.routers.tests.test_validator_concurrency import (
    _isolated_logging_salt as _isolated_logging_salt,
)
from grid_api.routers.tests.test_validator_concurrency import (
    _seed_validators,
    _workers,
)
from grid_api.routers.tests.test_validator_concurrency import (
    pg as pg,
)
from grid_api.services import validators as svc
from grid_api.v2.schema import validator_assignments as assignments
from grid_api.v2.schema import validator_attestations as attestations
from grid_api.v2.schema import validator_probe_groups as groups

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("VALIDATORS_TEST_DB_URL", "").startswith("postgresql"),
        reason="requires disposable PostgreSQL",
    ),
]


async def prepare(count=1):
    validators = await _seed_validators(count)
    workers = _workers()
    prepared = []
    for account, validator, wallet, key in validators:
        issued = await svc.issue_assignments(
            account_id=account,
            validator_id=validator,
            validator_wallet=wallet,
            active_workers=workers,
            limit=1,
        )
        assignment = issued["assignments"][0]
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
            "evidence_hash": "a" * 64,
            "verdict": "healthy",
            "score": 1.0,
            "latency_ms": 10,
        }
        async with await database.new_session() as session:
            await session.execute(
                sa.update(assignments)
                .where(assignments.c.id == assignment["assignment_id"])
                .values(probe_status="completed", probe_evidence_hash="a" * 64, probe_verdict="healthy"),
            )
            await session.commit()
        prepared.append((account, validator, key, payload))
    return prepared


def sign(payload, key):
    return Account.sign_message(encode_defunct(text=svc._canonical(payload)), key).signature.hex()


async def submit(prepared, *, payload=None, signature=None, account=None, validator=None):
    owner, node, key, original = prepared
    payload = original if payload is None else payload
    return await svc.record_attestation(
        account_id=owner if account is None else account,
        validator_id=node if validator is None else validator,
        payload=payload,
        signature=signature or sign(payload, key),
    )


async def snapshot():
    async with await database.new_session() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(attestations))
        group = (await session.execute(sa.select(groups))).mappings().one()
        return count, group["quorum_status"], group["quorum_outcome"]


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("grid_nonce", "incorrect-nonce", "grid_nonce"),
        ("evidence_hash", "b" * 64, "evidence_hash"),
        ("worker_id", str(uuid.UUID(int=9)), "worker_id"),
        ("model", "wrong-model", "model"),
        ("modality", "image", "modality"),
        ("capability", "wrong-capability", "capability"),
        ("canary_kind", "wrong-canary", "canary_kind"),
        ("probe_group_id", "pg_not_the_assigned_group", "probe_group_id"),
        ("assignment_id", "as_not_issued", "Grid-issued"),
    ],
)
async def test_resigned_wrong_binding_rejected_without_writes(pg, field, value, match):
    prepared = (await prepare())[0]
    forged = {**prepared[3], field: value}
    with pytest.raises(svc.AttestationError, match=match):
        await submit(prepared, payload=forged)
    assert await snapshot() == (0, "pending", None)
    assert (await submit(prepared))["status"] == "accepted"
    assert await snapshot() == (1, "pending", None)


@pytest.mark.parametrize("attack", ["wrong-key", "changed-signed-body", "other-account", "other-validator"])
async def test_identity_and_signature_rejected(pg, attack):
    first, second = await prepare(2)
    kwargs = {}
    if attack == "wrong-key":
        kwargs["signature"] = sign(first[3], second[2])
    elif attack == "changed-signed-body":
        kwargs = {"signature": sign(first[3], first[2]), "payload": {**first[3], "latency_ms": 999}}
    elif attack == "other-account":
        kwargs["account"] = second[0]
    else:
        kwargs["validator"] = second[1]
    with pytest.raises(svc.AttestationError):
        await submit(first, **kwargs)
    assert await snapshot() == (0, "pending", None)
    assert (await submit(first))["status"] == "accepted"


@pytest.mark.parametrize("attack", ["unfinished", "expired-assignment", "expired-group", "finalized-group"])
async def test_invalid_lifecycle_rejected(pg, attack):
    prepared = (await prepare())[0]
    old = svc._now() - timedelta(seconds=svc.ATTESTATION_GRACE_SECONDS + 60)
    table = groups if "group" in attack else assignments
    identity = prepared[3]["probe_group_id" if table is groups else "assignment_id"]
    values = {"expires": old}
    if attack == "unfinished":
        values = {"probe_status": "pending"}
    elif attack == "finalized-group":
        values = {"quorum_status": "finalized"}
    async with await database.new_session() as session:
        await session.execute(sa.update(table).where(table.c.id == identity).values(**values))
        await session.commit()
    before = await snapshot()
    with pytest.raises(svc.AttestationError):
        await submit(prepared)
    assert await snapshot() == before
    assert before[0] == 0


async def test_twenty_concurrent_retries_accept_once(pg):
    prepared = (await prepare())[0]
    results = await asyncio.gather(*(submit(prepared) for _ in range(20)))
    assert sum(row["status"] == "accepted" for row in results) == 1
    assert sum(row["status"] == "duplicate" for row in results) == 19
    assert len({row["id"] for row in results}) == 1
    assert await snapshot() == (1, "pending", None)
    assert (await submit(prepared))["status"] == "duplicate"
    assert await snapshot() == (1, "pending", None)


async def test_conflicting_concurrent_votes_from_one_validator_do_not_multiply(pg):
    prepared = (await prepare())[0]
    variants = [{**prepared[3], "verdict": verdict} for verdict in ["healthy", "failed"]]
    results = await asyncio.gather(*(submit(prepared, payload=payload) for payload in variants), return_exceptions=True)
    assert sum(isinstance(row, svc.AttestationError) for row in results) == 1
    assert sum(isinstance(row, dict) and row["status"] == "accepted" for row in results) == 1
    assert await snapshot() == (1, "pending", None)


async def test_disagreement_is_retained_not_silently_accepted(pg):
    prepared = await prepare(3)
    for entry, verdict in zip(prepared, ["healthy", "healthy", "failed"]):
        await submit(entry, payload={**entry[3], "verdict": verdict})
    count, status, _outcome = await snapshot()
    assert count == 3 and status == "disputed"
    async with await database.new_session() as session:
        stored = (await session.execute(sa.select(attestations.c.verdict))).scalars().all()
    assert sorted(stored) == ["failed", "healthy", "healthy"]
