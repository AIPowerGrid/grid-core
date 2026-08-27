# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compensated audits must look exactly like ordinary paid work to workers."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from grid_api.routers import worker_ws


class _WebSocket:
    def __init__(self, incoming: list[dict]):
        self.incoming = list(incoming)
        self.sent: list[dict] = []

    async def send_json(self, value: dict):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


def test_paid_audit_terminal_is_classified_as_ordinary_paid_work():
    assert worker_ws._is_paid_settlement("settled")
    assert worker_ws._is_paid_settlement("audit_settled")
    assert worker_ws._is_paid_settlement("no_reservation")
    assert not worker_ws._is_paid_settlement("audit_manual_review")
    assert worker_ws._requires_unpaid_terminal_error("audit_manual_review")


@pytest.mark.asyncio
async def test_media_audit_and_demand_have_identical_worker_transport(monkeypatch):
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "job_type": "image",
        "validator_audit_id": "aud_core_only",
        "validator_id": "val_core_only",
        "payload": {
            "prompt": "A copper circuit under clear glass",
            "seed": 17,
            "n": 1,
            "width": 1024,
            "height": 1024,
            "steps": 8,
        },
    }
    slots = [
        {
            "put_url": "https://upload.invalid/signed",
            "public_url": "https://media.example/image.webp",
            "key": f"image/{job_id}/0.webp",
            "content_type": "image/webp",
        },
    ]
    monkeypatch.setattr(time, "time", lambda: 100.0)
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: slots)
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)
    monkeypatch.setattr(worker_ws.signing, "verify_worker_sig", lambda *_a, **_k: None)

    settle_status = "settled"
    done_events: list[tuple[tuple, dict]] = []
    metric_events: list[dict] = []

    async def record_and_settle(**_kwargs):
        return settle_status

    async def publish_done(*args, **kwargs):
        done_events.append((args, kwargs))

    monkeypatch.setattr(worker_ws.credits, "record_and_settle", record_and_settle)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **kwargs: metric_events.append(kwargs))

    async def run(status: str):
        nonlocal settle_status
        settle_status = status
        done_events.clear()
        metric_events.clear()
        ws = _WebSocket(
            [
                {
                    "type": "done",
                    "results": [{"index": 0, "sha256": "a" * 64, "seed": 17}],
                },
            ],
        )
        finished = await worker_ws._handle_media_job(
            ws,
            job,
            "Krea 2 Turbo",
            "worker-ordinary",
            {"name": "image-rig", "wallet_address": "0x" + "1" * 40},
        )
        return finished, ws.sent, list(done_events), list(metric_events)

    demand = await run("settled")
    audit = await run("audit_settled")

    assert audit == demand
    finished, worker_messages, client_done, metrics = audit
    assert finished is True
    assert worker_messages[-1]["den"] > 0
    assert "validator" not in json.dumps(worker_messages).lower()
    assert "audit" not in json.dumps(worker_messages).lower()
    assert client_done
    assert metrics


@pytest.mark.asyncio
async def test_passthrough_audit_and_demand_have_identical_worker_transport(monkeypatch):
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "validator_audit_id": "aud_core_only",
        "validator_id": "val_core_only",
        "payload": {
            "api_format": "openai-responses",
            "max_length": 64,
            "request": {
                "model": "gpt-oss-120b",
                "input": "Explain why a mutex protects a critical section.",
                "max_output_tokens": 64,
            },
        },
    }
    incoming = [
        {
            "type": "done",
            "usage": {"input_tokens": 12, "output_tokens": 8},
            "full_json": {
                "id": "resp_ordinary",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "It serializes access."}]}],
            },
        },
    ]
    monkeypatch.setattr(time, "time", lambda: 100.0)

    settle_status = "settled"
    done_events: list[tuple[tuple, dict]] = []
    metric_events: list[dict] = []

    async def record_and_settle(**_kwargs):
        return settle_status

    async def publish_done(*args, **kwargs):
        done_events.append((args, kwargs))

    async def clear_strikes(_worker_id):
        return None

    monkeypatch.setattr(worker_ws.credits, "record_and_settle", record_and_settle)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws, "_clear_strikes", clear_strikes)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **kwargs: metric_events.append(kwargs))

    async def run(status: str):
        nonlocal settle_status
        settle_status = status
        done_events.clear()
        metric_events.clear()
        ws = _WebSocket(incoming)
        finished = await worker_ws._handle_raw_passthrough(
            ws,
            job,
            "gpt-oss-120b",
            "worker-ordinary",
            {
                "name": "text-rig",
                "wallet_address": "0x" + "2" * 40,
                "max_context_length": 131072,
            },
        )
        return finished, ws.sent, list(done_events), list(metric_events)

    demand = await run("settled")
    audit = await run("audit_settled")

    assert audit == demand
    finished, worker_messages, client_done, metrics = audit
    assert finished is True
    assert worker_messages[-1]["den"] > 0
    assert "validator" not in json.dumps(worker_messages).lower()
    assert "audit" not in json.dumps(worker_messages).lower()
    assert client_done
    assert metrics
