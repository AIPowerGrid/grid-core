# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real HTTP/WS/Redis/Postgres attack path; synthetic worker, not model fidelity.

Reuses the process-crash fixture's disposable database and isolated Core. No
production credentials or services are inherited. Registration and probes are
real; no completed assignments or attestation rows are inserted by the test.
"""

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import AsyncSession

from grid_api.routers.tests.test_core_process_crash import (
    frame,
    snapshot,
    worker,
)
from grid_api.routers.tests.test_core_process_crash import (
    pytestmark as pytestmark,
)
from grid_api.routers.tests.test_core_process_crash import (
    rig as rig,
)
from grid_api.routers.tests.validator_adversaries import RegexTemplateWorker
from grid_api.services import validator_compensation as compensation
from grid_api.services import validator_shadow as shadow
from grid_api.services import validators
from grid_api.v2 import schema as tables


def signed(payload, signer):
    return {
        "payload": payload,
        "signature": signer.sign_message(
            encode_defunct(text=validators._canonical(payload)),
        ).signature.hex(),
    }


async def register(client, core, engine, capability):
    signer, account = Account.create(), uuid.uuid4()
    key = "grid_" + secrets.token_urlsafe(32)
    wallet = signer.address.lower()
    async with engine.begin() as conn:
        await conn.execute(sa.insert(tables.accounts).values(id=account, wallet=wallet, flags={}))
        await conn.execute(sa.insert(tables.api_keys).values(
            hash=hashlib.sha256((core.env["GRID_SALT"] + key).encode()).hexdigest(),
            account_id=account,
            scopes=["validator.read", "validator.assignments", "validator.probe", "validator.attest"],
        ))
    headers = {"Authorization": "Bearer " + key}
    response = await client.post(core.url + "/v1/validator/register", headers=headers, json=signed({
        "registration_schema": "aipg.validator.registration.v1",
        "validator": wallet,
        "software_version": "v0.1.0-preview.20",
        "capabilities": [capability],
        "ts": int(time.time()),
    }, signer))
    assert response.status_code == 200, response.text
    return signer, account, response.json()["validator_id"], headers


async def respond(ws, broken):
    job = await frame(ws, "job")
    assert "_validator" not in json.dumps(job)
    reply = RegexTemplateWorker().respond(job["payload"]["request"])
    request = job["payload"]["request"]
    text, calls = reply.text, reply.tool_calls
    if broken:
        if calls:
            calls[0]["function"]["arguments"] = "{invalid-json"
        elif request.get("stop"):
            text += request["stop"] + "IGNORED_STOP"
        else:
            text = text[:1]
    delta = {"content": text}
    if calls:
        delta["tool_calls"] = [{**call, "index": index} for index, call in enumerate(calls)]
    await ws.send(json.dumps({"type": "token", "delta": delta}))
    await ws.send(json.dumps({
        "type": "done", "full_text": text, "finish_reason": reply.finish_reason,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }))
    ack = await frame(ws, "ack")
    assert ack["den"] == 0


@pytest.mark.parametrize("broken_worker", [False, True])
@pytest.mark.parametrize("false_votes", [False, True])
@pytest.mark.parametrize("capability", [
    "text.instruction.v1", "text.tool_call.v1", "text.stop_sequence.v1", "text.token_limit.v2",
])
async def test_signed_quorum_trust_boundaries(rig, monkeypatch, broken_worker, false_votes, capability):
    core, engine, _, customer, customer_key, worker_key = rig
    before = await snapshot(engine, customer)
    truthful = "failed" if broken_worker else "healthy"
    dishonest = "healthy" if broken_worker else "failed"
    submitted = dishonest if false_votes else truthful
    group_ids = set()
    async with httpx.AsyncClient(trust_env=False, timeout=40) as client:
        nodes = [await register(client, core, engine, capability) for _ in range(3)]
        ws = await worker(core, worker_key, "openai-chat")
        try:
            for signer, account, node_id, headers in nodes:
                response = await client.get(core.url + "/v1/validator/assignments?limit=1", headers=headers)
                assert response.status_code == 200, response.text
                assignment = response.json()["assignments"][0]
                assignment_id = assignment["assignment_id"]
                group_ids.add(assignment["probe_group_id"])
                premature = {
                    "attestation_schema": "aipg.validator.attestation.v0",
                    "assignment_source": "grid", "validator": signer.address.lower(),
                    "assignment_id": assignment_id,
                    "probe_group_id": assignment["probe_group_id"],
                    "grid_nonce": assignment["grid_nonce"], "evidence_hash": "0" * 64,
                    "worker_id": assignment["target_worker_id"], "model": assignment["model"],
                    "verdict": submitted,
                }
                url = core.url + "/v1/validator/attest"
                rejected = await client.post(url, headers=headers, json=signed(premature, signer))
                assert rejected.status_code == 400 and "not completed" in rejected.text
                probe, _ = await asyncio.gather(
                    client.post(core.url + "/v1/validator/probe/" + assignment_id, headers=headers),
                    respond(ws, broken_worker),
                )
                assert probe.status_code == 200, probe.text
                evidence = probe.json()
                payload = {
                    "attestation_schema": "aipg.validator.attestation.v0",
                    "assignment_source": "grid", "validator": signer.address.lower(),
                    **{key: evidence[key] for key in (
                        "assignment_id", "probe_group_id", "grid_nonce", "worker_id",
                        "model", "modality", "capability", "canary_kind", "evidence_hash",
                    )},
                    "verdict": submitted, "score": 1.0, "latency_ms": 1,
                }
                for key, value in (("evidence_hash", "0" * 64), ("grid_nonce", "made-up")):
                    rejected = await client.post(url, headers=headers, json=signed({**payload, key: value}, signer))
                    assert rejected.status_code == 400, rejected.text
                body = signed(payload, signer)
                unauthenticated = await client.post(url, json=body)
                assert unauthenticated.status_code == 401
                wrong_scope = await client.post(url, headers={"Authorization": "Bearer " + customer_key}, json=body)
                assert wrong_scope.status_code == 403
                other_signer, _, _, other_headers = next(item for item in nodes if item[2] != node_id)
                stolen = await client.post(url, headers=other_headers, json=signed(
                    {**payload, "validator": other_signer.address.lower()}, other_signer,
                ))
                assert stolen.status_code == 400 and "does not belong" in stolen.text
                accepted = await client.post(url, headers=headers, json=body)
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["status"] == "accepted"
                assert accepted.json()["economic_effect"] == "none"
                duplicate = await client.post(url, headers=headers, json=body)
                assert duplicate.json()["status"] == "duplicate"
                opposite = "failed" if submitted == "healthy" else "healthy"
                conflict = await client.post(url, headers=headers, json=signed({**payload, "verdict": opposite}, signer))
                assert conflict.status_code == 400, conflict.text
        finally:
            await ws.close()
        scorecards = await client.get(core.url + "/v1/validator/scorecards", headers=nodes[0][3])
        assert scorecards.status_code == 200, scorecards.text
        card = scorecards.json()["items"][0]
        assert card["total"] == 3
        assert card["verdict_verification"]["core_disagreed"] == (3 if false_votes else 0)
        assert card["verdict_verification"]["core_matched"] == (0 if false_votes else 3)
        assert card["quality_eligible"] is False

    assert len(group_ids) == 1
    async with engine.connect() as conn:
        votes = (await conn.execute(sa.select(tables.validator_attestations))).mappings().all()
        assignments = (await conn.execute(sa.select(tables.validator_assignments))).mappings().all()
        group = (await conn.execute(sa.select(tables.validator_probe_groups))).mappings().one()
    assert len(votes) == len(assignments) == 3
    assert group["quorum_status"] == "accepted"
    assert group["quorum_outcome"] == submitted
    assert {row["probe_verdict"] for row in assignments} == {truthful}
    by_id = {row["id"]: row for row in assignments}
    now = datetime.now(UTC)
    contract = {
        "scoring_policy": assignments[0]["scoring_policy_id"],
        "starts_at": (now - timedelta(hours=1)).isoformat(),
        "ends_at": (now + timedelta(hours=1)).isoformat(),
        "receipt_grace_seconds": 3600,
    }
    for vote in votes:
        member = {
            "account_id": str(vote["account_id"]), "validator_id": vote["validator_id"],
            "signing_wallet": vote["validator_wallet"], "operator_group_id": "isolated-test",
        }
        value, reason = compensation._verify_work(vote, by_id[vote["assignment_id"]], member, contract)
        if false_votes:
            assert value is None and reason == "incorrect_task_score"
        else:
            assert reason is None and value["attestation_id"] == vote["id"]

    # Advance only the parent verifier clock; fixture operator reviews are not
    # a claim that these synthetic identities have independent human owners.
    observed = group["expires"] + timedelta(seconds=validators.ATTESTATION_GRACE_SECONDS + 1)
    monkeypatch.setattr(validators, "_now", lambda: observed)
    async with AsyncSession(engine) as session:
        await validators._finalize_due_assignments(session)
        for index, (_, _, node_id, _) in enumerate(nodes):
            await session.execute(sa.update(tables.validators).where(tables.validators.c.id == node_id).values(
                operator_group_id=f"opg_isolated_{index}", independence_status="verified",
                independence_review_ref="test:synthetic-control-review",
                qualification_started_at=now - timedelta(days=5),
                independence_reviewed_at=now - timedelta(days=1),
                independence_expires_at=observed + timedelta(days=1), last_heartbeat=observed,
            ))
        await session.commit()
        config = shadow.frozen_policy_config({"validator_baseline_version": "v0.1.0-preview.20"})
        support = await shadow._authoritative_support_rows(session, observed_at=observed, config=config)
        assert len(support) == (0 if false_votes else 3)
        aggregate = shadow._aggregate_authoritative_rows(support)
        assert len(aggregate) == (0 if false_votes else 1)
        if aggregate:
            assert aggregate[0]["distinct_operator_count"] == 3
            assert aggregate[0]["outcome"] == truthful
    assert await snapshot(engine, customer) == before
