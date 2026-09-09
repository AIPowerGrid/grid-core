# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real, disposable Redis proofs for retry handoff; no production connections."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, ResponseError

from grid_api.services import job_queue as queue


@pytest_asyncio.fixture
async def redis(monkeypatch):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for retry handoff proof")
    with tempfile.TemporaryDirectory(prefix="aipg-q-", dir="/tmp") as directory:
        path = f"{directory}/redis.sock"
        process = subprocess.Popen(
            [
                binary,
                "--port",
                "0",
                "--unixsocket",
                path,
                "--unixsocketperm",
                "700",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=path, decode_responses=True, socket_timeout=3)
        try:
            for _ in range(100):
                assert process.poll() is None
                try:
                    if await client.ping():
                        break
                except ConnectionError:
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("disposable Redis did not start")
            for stream in (queue.STREAM_KEY, queue.MEDIA_STREAM_KEY):
                await client.xgroup_create(stream, queue.CONSUMER_GROUP, id="0", mkstream=True)
            monkeypatch.setattr(queue, "get_redis", lambda: client)
            monkeypatch.setattr(queue, "STALE_JOB_MS", 0)
            monkeypatch.setattr(queue, "STALE_JOB_MS_MEDIA", 0)
            yield client
        finally:
            await client.aclose()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def pending_job(job_type="text", **kwargs):
    await queue.submit_job(
        str(uuid4()),
        {"prompt": "synthetic retry proof"},
        ["test-model"],
        job_type=job_type,
        progress_token="test-progress",
        preferred_worker="preferred",
        hard_target_worker="target",
        **kwargs,
    )
    return await queue.pop_job("test-worker", timeout_ms=1, job_types=[job_type])


async def retry(job, mode):
    if mode == "mismatch":
        return await queue.requeue_for_mismatch(job)
    if mode == "affinity":
        return await queue.bounce_for_affinity(job)
    if mode == "stale":
        return await queue.claim_stale_jobs()
    return await queue.requeue_job(
        job["job_id"],
        job["payload"],
        job["models"],
        job["stream_id"],
        job_type=job["job_type"],
        stream=job["stream"],
        max_attempts=queue.MAX_GENERATION_REQUEUE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mismatch", "affinity", "failure", "stale"])
@pytest.mark.parametrize("job_type", ["text", "audio"])
async def test_failed_append_keeps_original_pending(redis, mode, job_type):
    job = await pending_job(job_type)
    # A real Redis XADD error, inside or outside Lua, without mocking the writer.
    await redis.xadd(job["stream"], {"test": "exhaust-id"}, id="18446744073709551615-18446744073709551615")
    if mode == "stale":
        assert await retry(job, mode) == 0  # reclaimer logs and retains its claim
    else:
        with pytest.raises(ResponseError):
            await retry(job, mode)
    pending = await redis.xpending_range(job["stream"], queue.CONSUMER_GROUP, "-", "+", 10)
    assert [row["message_id"] for row in pending] == [job["stream_id"]]


@pytest.mark.asyncio
async def test_affinity_limit_retains_running_claim(redis):
    job = await pending_job(affinity_passes=queue.MAX_AFFINITY_BOUNCE)
    assert await retry(job, "affinity") is False
    assert (await redis.xpending(job["stream"], queue.CONSUMER_GROUP))["pending"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mismatch", "affinity", "failure", "stale"])
async def test_handoff_preserves_original_metadata(redis, mode):
    job = await pending_job("video", requeue_count=7, affinity_passes=2)
    assert await retry(job, mode)
    replacement = await queue.pop_job("replacement", timeout_ms=1, job_types=["video"])
    for field in ("job_id", "job_type", "payload", "models", "progress_token", "hard_target_worker", "preferred_worker"):
        assert replacement[field] == job[field]
    assert replacement["requeue_count"] == 7 + (mode in ("mismatch", "failure"))
    assert replacement["affinity_passes"] == 2 + (mode == "affinity")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mismatch", "affinity", "failure"])
async def test_concurrent_retries_transfer_once_and_never_report_terminal(redis, mode):
    job = await pending_job()
    results = await asyncio.gather(*(retry(job, mode) for _ in range(25)))
    assert all(results), "a duplicate transfer must not tell the caller to refund"
    assert await redis.xlen(job["stream"]) == 2
    assert (await redis.xpending(job["stream"], queue.CONSUMER_GROUP))["pending"] == 0


@pytest.mark.asyncio
async def test_generation_budget_survives_legacy_counter_expiry(redis):
    job = await pending_job()
    for _ in range(queue.MAX_GENERATION_REQUEUE):
        assert await retry(job, "failure")
        await redis.delete(f"grid:requeue:{job['job_id']}")
        job = await queue.pop_job("next-worker", timeout_ms=1)
    assert await retry(job, "failure") is None
    assert await redis.xlen(job["stream"]) == 3
    assert (await redis.xpending(job["stream"], queue.CONSUMER_GROUP))["pending"] == 0


@pytest.mark.asyncio
async def test_mismatch_limit_is_inclusive_and_terminal_once(redis):
    job = await pending_job(requeue_count=queue.MAX_REQUEUE - 1)
    assert await retry(job, "mismatch") is True
    job = await queue.pop_job("next-worker", timeout_ms=1)
    assert await retry(job, "mismatch") is False
    assert await retry(job, "mismatch") is True  # no second terminal/refund
    assert await redis.xlen(job["stream"]) == 2


@pytest.mark.asyncio
async def test_old_release_counter_is_respected(redis):
    job = await pending_job()
    await redis.set(f"grid:requeue:{job['job_id']}", 2, ex=600)
    assert await retry(job, "failure") is None
    assert await redis.xlen(job["stream"]) == 1


@pytest.mark.asyncio
async def test_stale_handoff_does_not_take_a_refreshed_worker_claim(redis):
    job = await pending_job()
    await redis.xclaim(job["stream"], queue.CONSUMER_GROUP, "reclaimer", 0, [job["stream_id"]])
    await redis.xclaim(job["stream"], queue.CONSUMER_GROUP, "still-running", 0, [job["stream_id"]])
    status, _ = await queue._handoff({**job, "worker_id": "reclaimer"}, "stale")
    assert status == "superseded"
    assert await redis.xlen(job["stream"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_counter", ["not-an-integer", "wrong-type"])
async def test_bad_legacy_counter_does_not_remove_delivery(redis, bad_counter):
    job = await pending_job()
    key = f"grid:requeue:{job['job_id']}"
    if bad_counter == "wrong-type":
        await redis.hset(key, "field", "value")
    else:
        await redis.set(key, bad_counter)
    with pytest.raises(ResponseError):
        await retry(job, "failure")
    assert await redis.xlen(job["stream"]) == 1
    assert (await redis.xpending(job["stream"], queue.CONSUMER_GROUP))["pending"] == 1


_CHILD_RETRY = """
import asyncio, json, sys
from redis.asyncio import Redis
from grid_api.services import job_queue as queue

async def main():
    client = Redis(unix_socket_path=sys.argv[1], decode_responses=True)
    phase, mode, job = sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    class PausedClient:
        async def eval(self, *args):
            if phase == 'after':
                await client.eval(*args)
            print('handoff-paused', flush=True)
            await asyncio.Event().wait()
    queue.get_redis = lambda: PausedClient()
    if mode == 'mismatch':
        await queue.requeue_for_mismatch(job)
    elif mode == 'affinity':
        await queue.bounce_for_affinity(job)
    elif mode == 'stale':
        await queue._handoff(job, 'stale')
    else:
        await queue.requeue_job(job['job_id'], job['payload'], job['models'],
                                job['stream_id'], stream=job['stream'])

asyncio.run(main())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("mode", ["mismatch", "affinity", "failure", "stale"])
@pytest.mark.parametrize("job_type", ["text", "video"])
async def test_coordinator_queue_process_kill_recovers_once(redis, phase, mode, job_type):
    job = await pending_job(job_type)
    root = Path(__file__).resolve().parents[3]
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD_RETRY,
        redis.connection_pool.connection_kwargs["path"],
        phase,
        mode,
        json.dumps(job),
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 10) == b"handoff-paused\n"
        child.kill()
        await asyncio.wait_for(child.wait(), 5)
        assert child.returncode < 0
        # A new coordinator uses the same Redis state after losing all local state.
        recovered = await queue.claim_stale_jobs()
        assert recovered == (phase == "before")
        replacement = await queue.pop_job("restarted", timeout_ms=1, job_types=[job_type])
        assert replacement["job_id"] == job["job_id"]
        assert replacement["progress_token"] == "test-progress"
        assert await queue.pop_job("no-duplicate", timeout_ms=1, job_types=[job_type]) is None
        await queue.ack_job(replacement["stream_id"], replacement["stream"])
        assert (await redis.xpending(job["stream"], queue.CONSUMER_GROUP))["pending"] == 0
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()
