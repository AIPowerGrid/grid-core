# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A busy worker must not read the next job off the shared stream.

Real dispatch code and isolated PG for the reservation; queue, registry and
client delivery are fixtures, and the worker never answers its first job.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from grid_api.routers import worker_ws as transport
from grid_api.routers.tests.test_worker_billing_failures_postgres import Worker, reserve
from grid_api.routers.tests.test_worker_billing_failures_postgres import pg as pg  # noqa: F401

PG = os.environ.get("CREDITS_TEST_DB_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG.startswith("postgresql"), reason="requires disposable CREDITS_TEST_DB_URL"),
]


class BusyWorker(Worker):
    """Registers, receives a job, and then never says anything again."""

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.Future()


class AnsweringWorker(BusyWorker):
    """Answers each job it is sent, one token and a done."""

    def __init__(self, job, incoming):
        super().__init__(job, incoming)
        self.answers = asyncio.Queue()

    async def send_json(self, message):
        await super().send_json(message)
        if message["type"] == "job":
            for reply in (
                {"type": "token", "id": message["id"], "delta": {"content": "hello"}},
                {"type": "done", "id": message["id"], "full_text": "hello", "finish_reason": "stop"},
            ):
                self.answers.put_nowait(reply)

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        return await self.answers.get()


async def run_worker(pg, monkeypatch, worker_type):  # noqa: F811
    account, first, _ = await reserve(pg, "openai-chat")
    _, second, _ = await reserve(pg, "openai-chat")
    waiting = [first, second]
    reads = []

    async def pop(*_args, **_kwargs):
        reads.append(len(waiting))
        if waiting:
            return waiting.pop(0)
        await asyncio.Future()

    @asynccontextmanager
    async def claim(_job):
        yield

    monkeypatch.setattr(transport.accounts_svc, "resolve_api_key", AsyncMock(return_value={
        "id": str(account), "account_id": account, "source": "v2", "scopes": ["worker.connect"],
    }))
    monkeypatch.setattr(transport.worker_identity_svc, "verify_registration", AsyncMock(return_value=None))
    monkeypatch.setattr(transport, "_requires_managed_profile", lambda *_: False)
    for name in ("register_worker", "unregister_worker", "refresh_worker", "_clear_strikes"):
        monkeypatch.setattr(transport, name, AsyncMock())
    monkeypatch.setattr(transport, "_is_in_cooldown", AsyncMock(return_value=False))
    monkeypatch.setattr(transport.job_queue, "pop_job", pop)
    monkeypatch.setattr(transport.job_queue, "requeue_job", AsyncMock(return_value="2-0"))
    monkeypatch.setattr(transport.job_queue, "ack_job", AsyncMock())
    monkeypatch.setattr(transport.job_queue, "maintain_job_claim", claim)
    for name in ("publish_error", "publish_done", "publish_token", "publish_raw_event"):
        monkeypatch.setattr(transport.token_stream, name, AsyncMock())
    monkeypatch.setattr(transport.token_stream, "is_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(transport.route_events, "capture_route", lambda **_: None)
    monkeypatch.setattr(transport.route_events, "capture_outcome", lambda **_: None)

    worker = worker_type(first, [])
    runner = asyncio.create_task(transport.worker_websocket(worker))
    try:
        for _ in range(200):
            if any(message["type"] == "job" for message in worker.sent):
                break
            await asyncio.sleep(0.01)
        # Give the poll task every chance to read again.
        await asyncio.sleep(0.5)
        assert not runner.done(), runner
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    return first, second, waiting, reads, [m["id"] for m in worker.sent if m["type"] == "job"]


async def test_worker_with_a_job_in_flight_does_not_take_another(pg, monkeypatch):  # noqa: F811
    first, second, waiting, reads, sent = await run_worker(pg, monkeypatch, BusyWorker)

    assert sent == [first["job_id"]]
    assert reads == [2], "the busy worker read the stream again"
    assert waiting == [second]


async def test_worker_reads_the_next_job_once_the_first_is_finished(pg, monkeypatch):  # noqa: F811
    first, second, waiting, reads, sent = await run_worker(pg, monkeypatch, AnsweringWorker)

    assert sent == [first["job_id"], second["job_id"]]
    assert not waiting and reads == [2, 1, 0]
