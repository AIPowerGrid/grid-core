# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for job dispatch requeue logic.

The core correctness property: a job that lands on a worker which doesn't
serve its model must be requeued for another worker, NOT discarded — and a
job that no worker serves must eventually fault (not bounce forever).

Uses a fake Redis that records xadd / xack calls so we can assert the
requeue + bounce-limit behavior without a live Redis.
"""

from __future__ import annotations

import pytest

from grid_api.services import job_queue


class FakeRedis:
    def __init__(self):
        self.xadds: list[dict] = []
        self.xacks: list[str] = []

    async def xadd(self, stream, data):
        self.xadds.append(data)
        return f"fake-{len(self.xadds)}"

    async def xack(self, stream, group, msg_id):
        self.xacks.append(msg_id)
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(job_queue, "get_redis", lambda: r)
    return r


def _job(requeue_count=0, stream_id="s-1"):
    return {
        "stream_id": stream_id,
        "job_id": "job-1",
        "payload": {"prompt": "hi"},
        "models": ["llama-70b"],
        "requeue_count": requeue_count,
    }


@pytest.mark.asyncio
async def test_mismatch_requeues_and_acks(fake_redis):
    """A fresh mismatched job is acked (leaves this worker) and re-added."""
    requeued = await job_queue.requeue_for_mismatch(_job(requeue_count=0))

    assert requeued is True
    assert fake_redis.xacks == ["s-1"], "must ack the current delivery"
    assert len(fake_redis.xadds) == 1, "must re-add the job"
    assert fake_redis.xadds[0]["job_id"] == "job-1"
    assert fake_redis.xadds[0]["requeue_count"] == "1", "bounce counter increments"


@pytest.mark.asyncio
async def test_requeue_count_increments_each_bounce(fake_redis):
    await job_queue.requeue_for_mismatch(_job(requeue_count=5))
    assert fake_redis.xadds[0]["requeue_count"] == "6"


@pytest.mark.asyncio
async def test_bounce_limit_faults_instead_of_requeue(fake_redis):
    """At the limit, the job is acked but NOT re-added — caller faults it."""
    requeued = await job_queue.requeue_for_mismatch(_job(requeue_count=job_queue.MAX_REQUEUE))

    assert requeued is False, "signals caller to fault + notify client"
    assert fake_redis.xacks == ["s-1"], "still acked so it leaves the PEL"
    assert fake_redis.xadds == [], "must NOT re-add past the limit"


@pytest.mark.asyncio
async def test_bounce_limit_is_inclusive(fake_redis):
    """One below the limit still requeues; at the limit it stops."""
    below = await job_queue.requeue_for_mismatch(_job(requeue_count=job_queue.MAX_REQUEUE - 1))
    assert below is True
    assert len(fake_redis.xadds) == 1

    at = await job_queue.requeue_for_mismatch(_job(requeue_count=job_queue.MAX_REQUEUE))
    assert at is False
    assert len(fake_redis.xadds) == 1, "no new add at the limit"


@pytest.mark.asyncio
async def test_submit_job_carries_requeue_count(fake_redis):
    await job_queue.submit_job("job-2", {"p": 1}, ["m"], requeue_count=3)
    assert fake_redis.xadds[0]["requeue_count"] == "3"


@pytest.mark.asyncio
async def test_submit_job_defaults_requeue_count_zero(fake_redis):
    await job_queue.submit_job("job-3", {"p": 1}, ["m"])
    assert fake_redis.xadds[0]["requeue_count"] == "0"
