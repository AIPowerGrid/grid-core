# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import uuid

import pytest

from grid_api.routers import worker_ws


class _WebSocket:
    def __init__(self, incoming=None):
        self.sent = []
        self.incoming = list(incoming or [])

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


def test_validator_economic_bypass_requires_core_hard_target_metadata():
    job = {
        "job_type": "image",
        "hard_target_worker": "rig-a",
        "payload": {
            "_validator_probe": True,
            "_validator_assignment_id": "asg-secret",
            "_validator_probe_group_id": "prg-secret",
            "_validator_grid_nonce": "nonce-secret",
            "_validator_role": "candidate",
        },
    }
    assert worker_ws._is_assignment_bound_validator_job(job)

    job["hard_target_worker"] = ""
    assert not worker_ws._is_assignment_bound_validator_job(job)
    job["hard_target_worker"] = "rig-a"
    job["payload"].pop("_validator_probe_group_id")
    assert not worker_ws._is_assignment_bound_validator_job(job)


@pytest.mark.asyncio
async def test_validator_probe_hides_assignment_metadata_in_worker_dispatch(monkeypatch):
    ws = _WebSocket()
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "payload": {
            "request": {"model": "model-a", "messages": []},
            "api_format": "openai-chat",
            "prompt": "ordinary-looking request",
            "max_length": 32,
            "temperature": 0,
            "_validator_probe": True,
            "_validator_assignment_id": "asg-secret",
            "_validator_probe_group_id": "prg-secret",
            "_validator_grid_nonce": "nonce-secret",
        },
    }

    async def generation(*_args):
        return {
            "client_error": None,
            "failed": False,
            "grid_meta": {},
            "full_text": "ok",
            "full_reasoning": "",
            "tool_calls": [],
            "usage": {},
            "finish_reason": "stop",
        }

    published = []

    async def publish_done(*args, **kwargs):
        published.append((args, kwargs))

    monkeypatch.setattr(worker_ws, "_handle_worker_generation", generation)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)

    assert await worker_ws._handle_validator_probe(
        ws,
        job,
        "model-a",
        "worker-secret",
        {"name": "rig-a"},
    )

    dispatched = ws.sent[0]
    assert dispatched["id"] == job_id
    assert str(uuid.UUID(dispatched["id"])) == job_id
    assert dispatched["payload"] == {
        "request": {"model": "model-a", "messages": []},
        "api_format": "openai-chat",
        "prompt": "ordinary-looking request",
        "max_length": 32,
        "temperature": 0,
    }
    assert "validator" not in str(dispatched).lower()
    assert published[0][1]["grid"] == {
        "worker_id": "worker-secret",
        "assignment_id": "asg-secret",
        "grid_nonce": "nonce-secret",
        "economic_effect": "none",
    }
    assert ws.sent[-1] == {"type": "ack", "id": job_id, "den": 0}


@pytest.mark.asyncio
async def test_validator_worker_failure_becomes_signed_evidence_candidate(monkeypatch):
    ws = _WebSocket()
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "payload": {
            "request": {"model": "model-a", "messages": [{"role": "user", "content": "x"}]},
            "_validator_probe": True,
            "_validator_assignment_id": "asg-failed",
            "_validator_grid_nonce": "nonce-failed",
        },
    }

    async def generation(*_args):
        return {
            "client_error": None,
            "failed": True,
            "usage": None,
        }

    published = []

    async def publish_done(*args, **kwargs):
        published.append((args, kwargs))

    async def publish_error(*_args, **_kwargs):
        raise AssertionError("an accepted target-worker failure is evidence, not a transport error")

    monkeypatch.setattr(worker_ws, "_handle_worker_generation", generation)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)

    assert await worker_ws._handle_validator_probe(
        ws, job, "model-a", "worker-failed", {"name": "rig-failed"}
    )
    assert published[0][1]["finish_reason"] == "error"
    assert published[0][1]["grid"] == {
        "worker_id": "worker-failed",
        "assignment_id": "asg-failed",
        "grid_nonce": "nonce-failed",
        "probe_failed": True,
        "economic_effect": "none",
    }
    assert ws.sent[-1] == {"type": "ack", "id": job_id, "den": 0}


@pytest.mark.asyncio
async def test_paid_validator_probe_commits_before_ordinary_den_ack(monkeypatch):
    ws = _WebSocket()
    job_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "payload": {
            "request": {"model": "model-a", "messages": []},
            "api_format": "openai-chat",
            "prompt": "ordinary-looking request",
            "max_length": 32,
            "temperature": 0,
            "_validator_probe": True,
            "_validator_assignment_id": "asg-paid",
            "_validator_probe_group_id": "prg-paid",
            "_validator_grid_nonce": "nonce-paid",
            "_validator_paid_audit": True,
        },
    }

    async def generation(*_args):
        return {
            "client_error": None,
            "failed": False,
            "grid_meta": {},
            "full_text": "a useful answer",
            "full_reasoning": "",
            "tool_calls": [],
            "usage": {},
            "finish_reason": "stop",
            "metered": 3,
            "ttft": 0.1,
            "worker_sig": None,
        }

    order = []

    async def settle(**kwargs):
        order.append(("settle", kwargs))
        return "settled", 1.75

    async def publish_done(*args, **kwargs):
        order.append(("done", (args, kwargs)))

    monkeypatch.setattr(worker_ws, "_handle_worker_generation", generation)
    monkeypatch.setattr(
        worker_ws.validator_audits,
        "settled_result",
        lambda _job_id: _async_result(None),
    )
    monkeypatch.setattr(worker_ws.validator_audits, "record_and_settle", settle)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.signing, "verify_worker_sig", lambda *_args: None)

    assert await worker_ws._handle_validator_probe(
        ws,
        job,
        "model-a",
        worker_id,
        {"name": "rig-a", "max_context_length": 4096, "wallet_address": ""},
    )
    assert [item[0] for item in order] == ["settle", "done"]
    dispatched = ws.sent[0]
    assert "validator" not in str(dispatched).lower()
    assert ws.sent[-1] == {"type": "ack", "id": job_id, "den": 1.75}
    assert order[1][1][1]["grid"]["economic_effect"] == "worker_compensated_audit"


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_status", ["error", "no_reservation", "worker_mismatch", "unexpected"])
async def test_paid_validator_probe_invalid_settlement_is_not_acked(monkeypatch, settle_status):
    ws = _WebSocket()
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "payload": {
            "prompt": "ordinary-looking request",
            "max_length": 32,
            "_validator_probe": True,
            "_validator_assignment_id": "asg-paid",
            "_validator_probe_group_id": "prg-paid",
            "_validator_grid_nonce": "nonce-paid",
            "_validator_paid_audit": True,
        },
    }

    async def generation(*_args):
        return {
            "client_error": None,
            "failed": False,
            "grid_meta": {},
            "full_text": "answer",
            "full_reasoning": "",
            "tool_calls": [],
            "usage": {},
            "finish_reason": "stop",
            "metered": 1,
            "ttft": 0.1,
            "worker_sig": None,
        }

    errors = []

    async def publish_error(*args, **kwargs):
        errors.append((args, kwargs))

    monkeypatch.setattr(worker_ws, "_handle_worker_generation", generation)
    monkeypatch.setattr(
        worker_ws.validator_audits,
        "settled_result",
        lambda _job_id: _async_result(None),
    )
    monkeypatch.setattr(
        worker_ws.validator_audits,
        "record_and_settle",
        lambda **_kwargs: _async_result((settle_status, 0.0)),
    )
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.signing, "verify_worker_sig", lambda *_args: None)

    assert not await worker_ws._handle_validator_probe(
        ws,
        job,
        "model-a",
        str(uuid.uuid4()),
        {"name": "rig-a", "max_context_length": 4096, "wallet_address": ""},
    )
    assert errors
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_paid_validator_probe_replays_committed_result_without_gpu_dispatch(monkeypatch):
    ws = _WebSocket()
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "payload": {
            "prompt": "synthetic request",
            "_validator_probe": True,
            "_validator_assignment_id": "asg-replay",
            "_validator_probe_group_id": "prg-replay",
            "_validator_grid_nonce": "nonce-replay",
            "_validator_paid_audit": True,
        },
    }
    terminal = {
        "worker_id": str(uuid.uuid4()),
        "grid_meta": {},
        "full_text": "durable answer",
        "full_reasoning": "",
        "tool_calls": [],
        "usage": {"completion_tokens": 3},
        "finish_reason": "stop",
    }
    published = []

    async def generation(*_args):
        raise AssertionError("a settled audit must not reach the GPU again")

    async def publish_done(*args, **kwargs):
        published.append((args, kwargs))

    monkeypatch.setattr(worker_ws, "_handle_worker_generation", generation)
    monkeypatch.setattr(
        worker_ws.validator_audits,
        "settled_result",
        lambda _job_id: _async_result((terminal, 1.5)),
    )
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)

    assert await worker_ws._handle_validator_probe(
        ws,
        job,
        "model-a",
        str(uuid.uuid4()),
        {"name": "rig-a"},
    )
    assert ws.sent == []
    assert published[0][0][1] == "durable answer"
    assert published[0][1]["grid"] == {
        "worker_id": terminal["worker_id"],
        "assignment_id": "asg-replay",
        "grid_nonce": "nonce-replay",
        "economic_effect": "worker_compensated_audit",
    }


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_validator_image_probe_freezes_output_without_economic_side_effects(monkeypatch):
    job_id = str(uuid.uuid4())
    ws = _WebSocket(
        incoming=[{
            "type": "done",
            "results": [{"index": 0, "sha256": "a" * 64, "seed": 7}],
        }]
    )
    job = {
        "job_id": job_id,
        "job_type": "image",
        "payload": {
            "prompt": "ordinary image prompt",
            "seed": 7,
            "n": 1,
            "ext": "webp",
            "recipe_spec": {"1": {"inputs": {"text": "ordinary image prompt"}}},
            "_validator_probe": True,
            "_validator_assignment_id": "asg-secret",
            "_validator_probe_group_id": "prg-secret",
            "_validator_grid_nonce": "nonce-secret",
            "_validator_role": "candidate",
        },
    }
    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        lambda *_args, **_kwargs: [{
            "put_url": "https://upload.invalid/signed",
            "key": f"image/{job_id}/0.webp",
            "content_type": "image/webp",
        }],
    )
    monkeypatch.setattr(
        worker_ws.storage,
        "freeze_validator_output",
        lambda *_args, **_kwargs: {
            "key": f"validator/{job_id}/0.webp",
            "url": f"https://media.example/validator/{job_id}/0.webp",
            "sha256": "b" * 64,
            "bytes": 123,
            "content_type": "image/webp",
        },
    )
    monkeypatch.setattr(
        worker_ws,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "validator_media_max_output_bytes": 1024,
                "validator_media_probe_timeout_seconds": 600,
            },
        )(),
    )
    published = []

    async def publish_done(*args, **kwargs):
        published.append((args, kwargs))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("validator media probes must never touch economics")

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("validator media probes must never touch metrics")

    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.credits, "record_and_settle", forbidden)
    monkeypatch.setattr(worker_ws.credits, "release_job", forbidden)
    monkeypatch.setattr(worker_ws, "record_job_complete", forbidden_sync)
    monkeypatch.setattr(worker_ws, "record_job_failed", forbidden_sync)

    assert await worker_ws._handle_validator_media_probe(
        ws,
        job,
        "deterministic-checkpoint",
        "worker-candidate",
    )

    dispatched = ws.sent[0]
    assert dispatched["id"] == job_id
    assert "validator" not in str(dispatched).lower()
    assert dispatched["payload"]["prompt"] == "ordinary image prompt"
    assert ws.sent[-1] == {"type": "ack", "id": job_id, "den": 0}
    body = json.loads(published[0][0][1])
    assert body["witness"]["sha256"] == "b" * 64
    assert body["witness"]["role"] == "candidate"
    assert published[0][1]["grid"]["economic_effect"] == "none"


@pytest.mark.asyncio
async def test_validator_video_probe_freezes_mp4_without_economic_side_effects(monkeypatch):
    job_id = str(uuid.uuid4())
    ws = _WebSocket(
        incoming=[{
            "type": "done",
            "results": [{"index": 0, "sha256": "a" * 64, "seed": 9}],
        }]
    )
    job = {
        "job_id": job_id,
        "job_type": "video",
        "hard_target_worker": "video-rig",
        "payload": {
            "prompt": "ordinary video prompt",
            "seed": 9,
            "n": 1,
            "ext": "mp4",
            "recipe_spec": {"1": {"inputs": {"text": "ordinary video prompt"}}},
            "_validator_probe": True,
            "_validator_assignment_id": "asg-video",
            "_validator_probe_group_id": "prg-video",
            "_validator_grid_nonce": "nonce-video",
            "_validator_role": "candidate",
        },
    }
    assert worker_ws._is_assignment_bound_validator_job(job)
    job["payload"]["_validator_role"] = "reference"
    assert not worker_ws._is_assignment_bound_validator_job(job)
    job["payload"]["_validator_role"] = "candidate"

    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        lambda *_args, **_kwargs: [{
            "put_url": "https://upload.invalid/signed",
            "key": f"video/{job_id}/0.mp4",
            "content_type": "video/mp4",
        }],
    )
    monkeypatch.setattr(
        worker_ws.storage,
        "freeze_validator_output",
        lambda *_args, **_kwargs: {
            "key": f"validator/{job_id}/0.mp4",
            "url": f"https://media.example/validator/{job_id}/0.mp4",
            "sha256": "b" * 64,
            "bytes": 456,
            "content_type": "video/mp4",
        },
    )
    monkeypatch.setattr(
        worker_ws,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "validator_media_max_output_bytes": 1024,
                "validator_media_probe_timeout_seconds": 600,
            },
        )(),
    )
    published = []

    async def publish_done(*args, **kwargs):
        published.append((args, kwargs))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("validator video probes must never touch economics")

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("validator video probes must never touch metrics")

    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.credits, "record_and_settle", forbidden)
    monkeypatch.setattr(worker_ws.credits, "release_job", forbidden)
    monkeypatch.setattr(worker_ws, "record_job_complete", forbidden_sync)
    monkeypatch.setattr(worker_ws, "record_job_failed", forbidden_sync)

    assert await worker_ws._handle_validator_media_probe(
        ws,
        job,
        "video-checkpoint",
        "worker-video",
    )

    dispatched = ws.sent[0]
    assert dispatched["job_type"] == "video"
    assert dispatched["upload"][0]["content_type"] == "video/mp4"
    assert "validator" not in str(dispatched).lower()
    body = json.loads(published[0][0][1])
    assert body["witness"]["content_type"] == "video/mp4"
    assert body["witness"]["role"] == "candidate"
    assert ws.sent[-1] == {"type": "ack", "id": job_id, "den": 0}
