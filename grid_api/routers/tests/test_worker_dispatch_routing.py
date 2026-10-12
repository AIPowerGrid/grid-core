# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real Redis compatibility handoff and SQL holds; no GPU or production I/O."""

import asyncio
import json
from uuid import uuid4

import pytest
from redis.exceptions import ResponseError

from grid_api.routers import worker_ws
from grid_api.services import credits, job_queue, media_jobs
from grid_api.services.tests.test_media_job_journal import MODEL, OPENING, counts, owner, prepared, snapshot
from grid_api.services.tests.test_media_job_journal import db as db
from grid_api.services.tests.test_media_job_journal import redis as redis
from grid_api.services.tests.test_media_job_journal import synthetic_dependencies as synthetic_dependencies

FEATURE = "async-video-resume-v1"


async def peer(monkeypatch, redis, *, worker_id=None, **changes):
    monkeypatch.setattr(worker_ws, "get_redis", lambda: redis)
    worker_id = worker_id or str(uuid4())
    info = {"name": worker_id, "dispatch_protocol": job_queue.ROUTING_PROTOCOL,
            "job_types": ["video"], "models": [MODEL],
            "media_features": [FEATURE], **changes}
    await worker_ws.register_worker(worker_id, info)
    return worker_id, json.dumps(info)


async def reserved_delivery(*, count=0, features=None):
    base = prepared(await owner())
    job = media_jobs.prepare(
        account_id=base.account_id, idempotency_key=str(uuid4()), request_hash=base.request_hash,
        model=base.model, payload={**base.payload, "required_media_features": [FEATURE] if features is None else features},
        route_models=base.route_models, max_cost_micro=base.max_cost_micro,
    )
    await media_jobs.reserve(job)
    await job_queue.submit_job(job.job_id, job.payload, job.route_models, job_type="video", requeue_count=count)
    queued = await job_queue.pop_job("wrong-reader", timeout_ms=1, job_types=["video"])
    return job, queued


@pytest.mark.asyncio
async def test_busy_compatible_peer_receives_original_job_without_bounces_or_refund(db, redis, monkeypatch):
    correct, _ = await peer(monkeypatch, redis)
    await job_queue.submit_job(str(uuid4()), {"prompt": "synthetic busy job"}, [MODEL], job_type="video")
    busy = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    job, queued = await reserved_delivery(count=job_queue.MAX_REQUEUE)
    original = await redis.xrange(job_queue.MEDIA_STREAM_KEY, queued["stream_id"], queued["stream_id"])
    balance = await credits.get_balance(job.account_id)

    assert await worker_ws._requeue_incompatible_job(queued) is True
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 2
    assert await redis.xrange(job_queue.MEDIA_STREAM_KEY, queued["stream_id"], queued["stream_id"]) == original
    assert await job_queue.pop_job("wrong-reader", timeout_ms=1, job_types=["video"]) is None
    assert await credits.get_balance(job.account_id) == balance
    journal, hold, debits = await snapshot(job)
    assert hold["status"] == "held" and journal["execution_started"] is None and len(debits) == 1

    await job_queue.ack_job(busy["stream_id"], busy["stream"])
    received = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    assert received["stream_id"] == queued["stream_id"] and received["payload"] == queued["payload"]
    assert received["requeue_count"] == job_queue.MAX_REQUEUE and not received["dispatch_deadline_expired"]
    assert received["routed_dispatch"] and await job_queue.routed_dispatch_status(received) == "ready"
    assert await media_jobs.claim_execution(
        job_id=job.job_id, payload=received["payload"], route_models=received["models"],
        preferred_worker="", selected_model=MODEL, worker_id=correct, job_type="video",
        execution_token=str(uuid4()),
    ) == "claimed"
    assert await counts() == (1, 1, 0)
    assert await credits.get_balance(job.account_id) == balance


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["model", "format", "modality", "features", "expired", "target", "legacy"])
async def test_only_one_fully_compatible_fresh_target_can_defer_terminal_release(db, redis, monkeypatch, missing):
    changes = {
        "model": {"models": ["other-model"]},
        "format": {"api_formats": ["anthropic"]},
        "modality": {"job_types": ["image"]},
        "features": {"media_features": []},
        "legacy": {"dispatch_protocol": None},
    }.get(missing, {})
    correct, _ = await peer(monkeypatch, redis, **changes)
    if missing == "expired":
        await redis.delete(f"grid:worker:{correct}:status")
    job, queued = await reserved_delivery(count=job_queue.MAX_REQUEUE)
    if missing == "target":
        queued["hard_target_worker"] = "different-target"
    assert await worker_ws._requeue_incompatible_job(queued) is False
    await worker_ws._release_undispatched_job(queued)
    assert await credits.get_balance(job.account_id) == OPENING
    assert await counts() == (1, 1, 0)
    assert (await snapshot(job))[1]["status"] == "settled"
    assert await redis.llen(job_queue._routing_inbox(job_queue.MEDIA_STREAM_KEY, correct)) == 0


@pytest.mark.asyncio
async def test_features_cannot_be_combined_from_different_workers(db, redis, monkeypatch):
    await peer(monkeypatch, redis, media_features=[FEATURE])
    await peer(monkeypatch, redis, media_features=["recipe-image-bindings-v1"])
    job, queued = await reserved_delivery(count=job_queue.MAX_REQUEUE, features=[FEATURE, "recipe-image-bindings-v1"])
    assert await worker_ws._requeue_incompatible_job(queued) is False
    await worker_ws._release_undispatched_job(queued)
    assert await credits.get_balance(job.account_id) == OPENING


@pytest.mark.asyncio
async def test_concurrent_routes_and_old_owner_retries_cannot_duplicate_delivery(db, redis, monkeypatch):
    correct, state = await peer(monkeypatch, redis)
    _job, queued = await reserved_delivery()
    results = await asyncio.gather(*(job_queue.route_to_worker(queued, correct, state) for _ in range(25)))
    assert results.count("routed") == 1 and results.count("superseded") == 24
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    assert await redis.llen(job_queue._routing_inbox(job_queue.MEDIA_STREAM_KEY, correct)) == 1
    assert await job_queue.requeue_for_mismatch(queued) is True
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    received = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    assert received["job_id"] == queued["job_id"]
    assert await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"]) is None


@pytest.mark.asyncio
async def test_registry_change_and_bad_inbox_leave_original_claim_owned(db, redis, monkeypatch):
    correct, state = await peer(monkeypatch, redis)
    _job, queued = await reserved_delivery()
    await worker_ws.refresh_worker(correct, {"name": correct, "models": ["different-model"]})
    assert await job_queue.route_to_worker(queued, correct, state) == "unavailable"
    await redis.set(f"grid:worker:{correct}:status", state, ex=60)
    await redis.set(job_queue._routing_inbox(job_queue.MEDIA_STREAM_KEY, correct), "not-a-list")
    with pytest.raises(ResponseError, match="invalid routing inbox"):
        await job_queue.route_to_worker(queued, correct, state)
    pending = await redis.xpending_range(job_queue.MEDIA_STREAM_KEY, job_queue.CONSUMER_GROUP, "-", "+", 10)
    assert len(pending) == 1 and pending[0]["consumer"] == queued["worker_id"]
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1


@pytest.mark.asyncio
async def test_old_notifications_cannot_redeliver_after_routing_back_to_a_peer(db, redis, monkeypatch):
    first, first_state = await peer(monkeypatch, redis)
    second, second_state = await peer(monkeypatch, redis)
    _job, queued = await reserved_delivery()
    assert await job_queue.route_to_worker(queued, first, first_state) == "routed"
    assert await job_queue.route_to_worker({**queued, "worker_id": first}, second, second_state) == "routed"
    assert await job_queue.route_to_worker({**queued, "worker_id": second}, first, first_state) == "routed"
    received = await job_queue.pop_job(first, timeout_ms=1, job_types=["video"])
    assert received["job_id"] == queued["job_id"]
    assert await job_queue.pop_job(first, timeout_ms=1, job_types=["video"]) is None
    assert await job_queue.pop_job(second, timeout_ms=1, job_types=["video"]) is None
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1


@pytest.mark.asyncio
async def test_expired_busy_inbox_closes_once_without_execution_or_payment(db, redis, monkeypatch):
    correct, state = await peer(monkeypatch, redis)
    job, queued = await reserved_delivery()
    assert await job_queue.route_to_worker(queued, correct, state) == "routed"
    monkeypatch.setattr(job_queue, "DISPATCH_WAIT_MS_MEDIA", 1)
    await asyncio.sleep(0.01)
    received = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    assert received["dispatch_deadline_expired"] is True
    assert (await redis.xpending(job_queue.MEDIA_STREAM_KEY, job_queue.CONSUMER_GROUP))["pending"] == 0
    await worker_ws._release_undispatched_job(received)
    assert await credits.get_balance(job.account_id) == OPENING
    assert (await snapshot(job))[0]["execution_started"] is None
    assert await counts() == (1, 1, 0)
    assert await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"]) is None


@pytest.mark.asyncio
async def test_expired_duplicate_cannot_refund_an_existing_execution(db, redis, monkeypatch):
    correct, state = await peer(monkeypatch, redis)
    job, queued = await reserved_delivery()
    assert await media_jobs.claim_execution(
        job_id=job.job_id, payload=job.payload, route_models=job.route_models, preferred_worker="",
        selected_model=MODEL, worker_id=correct, job_type="video", execution_token=str(uuid4()),
    ) == "claimed"
    balance = await credits.get_balance(job.account_id)
    assert await job_queue.route_to_worker(queued, correct, state) == "routed"
    monkeypatch.setattr(job_queue, "DISPATCH_WAIT_MS_MEDIA", 1)
    await asyncio.sleep(0.01)
    received = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    assert received["dispatch_deadline_expired"] is True
    await worker_ws._release_undispatched_job(received)
    assert await credits.get_balance(job.account_id) == balance
    journal, hold, _ = await snapshot(job)
    assert journal["execution_started"] is not None and hold["status"] == "held"
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_routed_prefetch_wait_is_included_in_dispatch_deadline(db, redis, monkeypatch):
    correct, state = await peer(monkeypatch, redis)
    job, queued = await reserved_delivery()
    assert await job_queue.route_to_worker(queued, correct, state) == "routed"
    received = await job_queue.pop_job(correct, timeout_ms=1, job_types=["video"])
    assert received["routed_dispatch"] and not received["dispatch_deadline_expired"]
    monkeypatch.setattr(job_queue, "DISPATCH_WAIT_MS_MEDIA", 1)
    await asyncio.sleep(0.01)
    assert await job_queue.routed_dispatch_status(received) == "closed"
    assert (await redis.xpending(job_queue.MEDIA_STREAM_KEY, job_queue.CONSUMER_GROUP))["pending"] == 0
    await worker_ws._release_undispatched_job(received)
    assert await credits.get_balance(job.account_id) == OPENING
    assert await job_queue.routed_dispatch_status(received) == "superseded"
    assert (await snapshot(job))[0]["execution_started"] is None


@pytest.mark.asyncio
async def test_superseded_prefetch_cannot_close_the_new_pending_owner(db, redis, monkeypatch):
    first, first_state = await peer(monkeypatch, redis)
    second, second_state = await peer(monkeypatch, redis)
    job, queued = await reserved_delivery()
    assert await job_queue.route_to_worker(queued, first, first_state) == "routed"
    received = await job_queue.pop_job(first, timeout_ms=1, job_types=["video"])
    assert await job_queue.route_to_worker(received, second, second_state) == "routed"
    monkeypatch.setattr(job_queue, "DISPATCH_WAIT_MS_MEDIA", 1)
    await asyncio.sleep(0.01)
    assert await job_queue.routed_dispatch_status(received) == "superseded"
    pending = await redis.xpending_range(job_queue.MEDIA_STREAM_KEY, job_queue.CONSUMER_GROUP, "-", "+", 10)
    assert len(pending) == 1 and pending[0]["consumer"] == second
    assert (await snapshot(job))[1]["status"] == "held"
    assert await counts() == (1, 1, 0)
