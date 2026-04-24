# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hybrid job queue: Redis (local) + Waku (network-wide).

This module provides a unified interface that:
1. Publishes jobs to Waku for network-wide distribution
2. Uses Redis for local worker management (WebSocket state)
3. Falls back to Redis-only if Waku is disabled

Workers connected to THIS node get jobs via Redis Streams.
Workers on OTHER nodes get jobs via Waku relay.

Usage:
    # Submit a job (goes to Waku + local Redis)
    await submit_job(job_id, payload, models)

    # Local worker pops from Redis (same as before)
    job = await pop_job(worker_id)

    # Claim prevents other nodes from processing
    await claim_job(job_id, worker_id)
"""

import json
import logging
import os

from ..redis_client import CONSUMER_GROUP, STREAM_KEY, get_redis

logger = logging.getLogger("grid_api.job_queue_hybrid")

# Feature flag
WAKU_ENABLED = os.getenv("WAKU_ENABLED", "false").lower() == "true"

# Lazy import Waku to avoid startup cost if disabled
_waku = None


def _get_waku():
    global _waku
    if _waku is None and WAKU_ENABLED:
        from .waku_queue import get_waku_queue
        _waku = get_waku_queue()
    return _waku


async def submit_job(job_id: str, payload: dict, models: list[str]) -> str:
    """
    Submit a job for processing.

    If Waku is enabled:
      - Broadcasts to network (all nodes see it)
      - Also adds to local Redis (for workers on this node)

    If Waku is disabled:
      - Adds to local Redis only (original behavior)
    """
    r = get_redis()
    data = {
        "job_id": job_id,
        "payload": json.dumps(payload),
        "models": json.dumps(models),
    }

    # Always add to local Redis for this node's workers
    stream_id = await r.xadd(STREAM_KEY, data)

    # If Waku enabled, also broadcast to network
    waku = _get_waku()
    if waku:
        try:
            await waku.submit_job(job_id, payload, models)
            logger.debug(f"[HYBRID] Job {job_id} published to Waku + Redis")
        except Exception as e:
            logger.warning(f"[HYBRID] Waku publish failed, Redis-only: {e}")
    else:
        logger.debug(f"[HYBRID] Job {job_id} published to Redis only")

    return stream_id


async def pop_job(worker_id: str, timeout_ms: int = 5000) -> dict | None:
    """
    Pop next job from local Redis stream.

    This is called by workers connected to THIS node.
    Workers on other nodes pop from their own Redis.
    """
    r = get_redis()
    results = await r.xreadgroup(
        CONSUMER_GROUP,
        worker_id,
        {STREAM_KEY: ">"},
        count=1,
        block=timeout_ms,
    )
    if not results:
        return None

    stream_name, messages = results[0]
    message_id, fields = messages[0]

    job_id = fields["job_id"]

    # Check if another node already claimed this job via Waku
    waku = _get_waku()
    if waku and waku.is_claimed(job_id):
        claim = waku.get_claim(job_id)
        if claim.worker_id != worker_id:
            # Someone else got it - ack and skip
            await r.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
            logger.debug(f"[HYBRID] Job {job_id} already claimed by {claim.worker_id}, skipping")
            return None

    return {
        "stream_id": message_id,
        "job_id": job_id,
        "payload": json.loads(fields["payload"]),
        "models": json.loads(fields["models"]),
    }


async def claim_job(job_id: str, worker_id: str) -> bool:
    """
    Claim a job for this worker.

    Broadcasts claim to Waku so other nodes know not to process it.
    Returns False if someone else already claimed it.
    """
    waku = _get_waku()
    if waku:
        return await waku.claim_job(job_id, worker_id)
    return True  # No Waku = no contention


async def ack_job(message_id: str):
    """Acknowledge a completed job (removes from Redis pending list)."""
    r = get_redis()
    await r.xack(STREAM_KEY, CONSUMER_GROUP, message_id)


async def requeue_job(job_id: str, payload: dict, models: list[str], stream_id: str = None):
    """Requeue a failed job."""
    r = get_redis()
    if stream_id:
        await r.xack(STREAM_KEY, CONSUMER_GROUP, stream_id)
    new_id = await submit_job(job_id, payload, models)
    logger.info(f"[HYBRID] Requeued job {job_id} as {new_id}")
    return new_id
