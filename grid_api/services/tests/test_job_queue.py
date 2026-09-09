# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue API adapter tests. Retry correctness is proved on real Redis next door."""

from unittest.mock import AsyncMock

import pytest

from grid_api.services import job_queue


@pytest.fixture
def fake_redis(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(job_queue, "get_redis", lambda: client)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 3])
async def test_submit_job_carries_requeue_count(fake_redis, count):
    await job_queue.submit_job("job", {"p": 1}, ["m"], requeue_count=count)
    assert fake_redis.xadd.call_args.args[1]["requeue_count"] == str(count)


@pytest.mark.asyncio
async def test_touch_job_claim_preserves_worker_ownership(fake_redis):
    await job_queue.touch_job_claim({"stream_id": "1-0", "worker_id": "worker-1", "job_type": "audio", "stream": "grid:jobs:media"})
    fake_redis.xclaim.assert_awaited_once_with(
        "grid:jobs:media",
        job_queue.CONSUMER_GROUP,
        "worker-1",
        min_idle_time=0,
        message_ids=["1-0"],
        justid=True,
    )


@pytest.mark.asyncio
async def test_requeue_requires_source_delivery(fake_redis):
    with pytest.raises(ValueError, match="pending stream delivery"):
        await job_queue.requeue_job("job", {}, [])
    fake_redis.eval.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,delivery,expected",
    [
        ("closed", "", None),
        ("requeued", "2-0", "2-0"),
        ("superseded", "1-0", "1-0"),
    ],
)
async def test_failure_status_only_dead_letter_returns_none(fake_redis, status, delivery, expected):
    fake_redis.eval.return_value = [status, delivery]
    assert await job_queue.requeue_job("job", {}, [], "1-0") == expected
