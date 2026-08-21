# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid

import pytest

from grid_api.routers import worker_ws


class _WebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, value):
        self.sent.append(value)


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
