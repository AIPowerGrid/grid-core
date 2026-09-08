# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real isolated Redis queue/replay qualification, not a live fleet attestation."""

import asyncio
import copy
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from grid_api.routers import worker_ws
from grid_api.services import job_queue, token_stream, validators


@pytest_asyncio.fixture
async def isolated_redis(monkeypatch):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for isolated transport proof")
    # Short Unix path avoids macOS sockaddr length limits; no listening TCP port.
    with tempfile.TemporaryDirectory(prefix="aipg-r-", dir="/tmp") as directory:
        path = f"{directory}/redis.sock"
        process = subprocess.Popen(
            [binary, "--port", "0", "--unixsocket", path, "--unixsocketperm", "700",
             "--save", "", "--appendonly", "no", "--dir", directory],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=path, decode_responses=True, socket_timeout=3)
        try:
            for _ in range(100):
                assert process.poll() is None, "isolated Redis exited before startup"
                try:
                    if await client.ping():
                        break
                except RedisConnectionError:
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("isolated Redis did not start")
            monkeypatch.setattr(job_queue, "get_redis", lambda: client)
            monkeypatch.setattr(token_stream, "get_redis", lambda: client)
            await client.xgroup_create(job_queue.STREAM_KEY, job_queue.CONSUMER_GROUP, id="0", mkstream=True)
            yield client
        finally:
            await client.aclose()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def round_trip(client, frames, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("qualification entered an economic/health path")

    for name in ["record_and_settle", "release_job"]:
        monkeypatch.setattr(worker_ws.credits, name, forbidden)
    for name in ["calculate_den", "record_job_complete", "record_job_failed", "_clear_strikes", "_handle_raw_passthrough"]:
        monkeypatch.setattr(worker_ws, name, forbidden)
    monkeypatch.setattr(worker_ws.ledger_svc, "record_completion", forbidden)
    job_id = str(uuid4())
    expected = copy.deepcopy(frames)
    for frame in expected:
        frame["id"] = job_id
    sent = []

    class Socket:
        async def receive_json(self):
            return expected.pop(0)

        async def send_json(self, value):
            sent.append(value)

        async def close(self, **_kwargs):
            pytest.fail("valid captured stream unexpectedly closed")

    async def consume():
        queued = await job_queue.pop_job("local-worker", timeout_ms=5000)
        assert queued["hard_target_worker"] == "local-rig"
        queued["worker_id"] = "local-worker"
        assert await worker_ws._maybe_handle_validator_text_probe(Socket(), queued, "gpt-oss-20b", "local-worker", {})
        await job_queue.ack_job(queued["stream_id"], stream=queued["stream"])

    consumer = asyncio.create_task(consume())
    try:
        stage = await validators._run_targeted_text_stage(
            row={"model": "gpt-oss-20b", "target_worker_id": "local-worker", "target_worker_name": "local-rig",
                 "probe_group_id": "prg-local", "grid_nonce": "nonce-local"},
            assignment_id="asg-local", job_id=job_id, prompt="local synthetic capture",
            request={"input": "local synthetic capture", "max_output_tokens": 256, "stream": True},
            api_format="openai-responses", timeout_seconds=10,
        )
        await asyncio.wait_for(consumer, timeout=10)
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
    assert not expected
    assert stage["status"] == "completed"
    assert stage["grid"]["assignment_id"] == "asg-local"
    assert stage["grid"]["grid_nonce"] == "nonce-local"
    assert stage["grid"]["worker_id"] == "local-worker"
    assert sent[-1] == {"type": "ack", "id": job_id, "den": 0}
    assert (await client.xpending(job_queue.STREAM_KEY, job_queue.CONSUMER_GROUP))["pending"] == 0
    replay = [event async for event in token_stream.subscribe_tokens(job_id, timeout=1)]
    assert len(replay) == 1
    assert replay[0]["logprobs"] == stage["logprobs"]
    native = [position for frame in frames if frame["type"] == "raw"
              for data in [json.loads(frame["data"])] if data.get("type") == "response.output_text.delta"
              for position in data.get("logprobs") or []]
    observed = [position for event in stage["logprobs"]["events"] for position in event["positions"]]
    assert native == observed
    assert native, "no native probabilities exercised"
    return stage


@pytest.mark.asyncio
async def test_queue_collector_redis_and_targeted_stage_preserve_probabilities(isolated_redis, monkeypatch):
    position = {"token": "Blue", "logprob": -0.25, "bytes": [66, 108, 117, 101],
                "top_logprobs": [{"token": "Green", "logprob": -2.5}]}
    raw = {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg-test",
           "output_index": 0, "content_index": 0, "delta": "Blue", "logprobs": [position]}
    frames = [{"type": "raw", "data": json.dumps(raw)},
              {"type": "raw", "data": '{"type":"response.completed"}'}, {"type": "done"}]
    stage = await round_trip(isolated_redis, frames, monkeypatch)
    assert stage["logprobs"]["status"] == "available"


@pytest.mark.asyncio
async def test_private_recorded_worker_capture(isolated_redis, monkeypatch):
    capture_path = os.environ.get("VALIDATOR_RESPONSES_CAPTURE")
    if not capture_path:
        pytest.skip("optional private capture replay; synthetic Redis proof runs independently")
    capture = json.loads(Path(capture_path).read_text())
    assert capture["source_events"] == [frame["data"] for frame in capture["worker_frames"] if frame["type"] == "raw"]
    stage = await round_trip(isolated_redis, capture["worker_frames"], monkeypatch)
    assert stage["logprobs"]["status"] in {"available", "partial"}
    print(json.dumps({
        "captured_backend_worker_events": len(capture["source_events"]),
        "positions_unchanged_through_real_redis": sum(len(event["positions"]) for event in stage["logprobs"]["events"]),
        "observation_status": stage["logprobs"]["status"],
        "live_core_dispatch": False, "validator_runtime_attestation": False,
    }))
