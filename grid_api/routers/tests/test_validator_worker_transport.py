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
async def test_validator_probe_is_indistinguishable_in_worker_transport(monkeypatch):
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
