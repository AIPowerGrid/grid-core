# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hybrid job queue: Redis + P2P.

This module provides a unified interface that:
1. Uses P2P gossipsub for job distribution when enabled
2. Falls back to Redis Streams when P2P is disabled
3. Can use both simultaneously for redundancy

Usage:
    from grid_api.services.p2p.hybrid_queue import (
        submit_job, pop_job, claim_job, ack_job
    )

    # Works the same regardless of backend
    await submit_job(job_id, payload, models)
"""

import logging
from typing import Any

from .config import get_p2p_config
from .node import get_p2p_node
from . import job_queue as p2p_queue
from ..job_queue import (
    submit_job as redis_submit_job,
    pop_job as redis_pop_job,
    ack_job as redis_ack_job,
    requeue_job as redis_requeue_job,
)

logger = logging.getLogger("grid_api.p2p.hybrid_queue")


async def submit_job(job_id: str, payload: dict, models: list[str]) -> str:
    """Submit a job for processing.

    Strategy:
    - If P2P enabled: broadcast to gossipsub AND add to local Redis
    - If P2P disabled: add to Redis only (original behavior)

    Adding to Redis ensures local workers (connected via WebSocket) get jobs
    even when P2P is enabled.
    """
    config = get_p2p_config()

    # Always add to Redis for local workers
    stream_id = await redis_submit_job(job_id, payload, models)

    # If P2P enabled, also broadcast to network
    if config.enabled and get_p2p_node():
        try:
            await p2p_queue.submit_job(job_id, payload, models)
            logger.debug(f"[HYBRID] Job {job_id} published to P2P + Redis")
        except Exception as e:
            logger.warning(f"[HYBRID] P2P publish failed, Redis-only: {e}")
    else:
        logger.debug(f"[HYBRID] Job {job_id} published to Redis only")

    return stream_id


async def pop_job(worker_id: str, timeout_ms: int = 5000) -> dict | None:
    """Pop the next job from the queue.

    For workers connected via WebSocket (local), we use Redis.
    For workers connected via P2P, they use p2p_queue.pop_job() directly.

    This function is for local (WebSocket) workers.
    """
    config = get_p2p_config()

    # Get job from Redis
    job = await redis_pop_job(worker_id, timeout_ms)
    if not job:
        return None

    # If P2P enabled, check if job was claimed by P2P worker
    if config.enabled and get_p2p_node():
        if await p2p_queue.is_claimed(job["job_id"]):
            claim = p2p_queue.get_claim(job["job_id"])
            if claim and claim.worker_id != worker_id:
                # Someone else claimed via P2P, skip
                await redis_ack_job(job["stream_id"])
                logger.debug(
                    f"[HYBRID] Job {job['job_id']} claimed by P2P worker {claim.worker_id}"
                )
                return None

    return job


async def claim_job(job_id: str, worker_id: str) -> bool:
    """Claim a job for this worker.

    Broadcasts claim to P2P network so remote workers skip it.
    Local workers (Redis) coordinate via stream consumer groups.
    """
    config = get_p2p_config()

    if config.enabled and get_p2p_node():
        return await p2p_queue.claim_job(job_id, worker_id)

    # No P2P = no contention (Redis handles it)
    return True


async def ack_job(message_id: str) -> None:
    """Acknowledge a completed job."""
    await redis_ack_job(message_id)

    config = get_p2p_config()
    if config.enabled:
        await p2p_queue.ack_job(message_id)


async def requeue_job(
    job_id: str, payload: dict, models: list[str], stream_id: str = None
) -> str:
    """Requeue a failed job."""
    config = get_p2p_config()

    # Always requeue to Redis
    new_id = await redis_requeue_job(job_id, payload, models, stream_id)

    # If P2P enabled, also requeue there
    if config.enabled and get_p2p_node():
        try:
            await p2p_queue.requeue_job(job_id, payload, models)
        except Exception as e:
            logger.warning(f"[HYBRID] P2P requeue failed: {e}")

    logger.info(f"[HYBRID] Requeued job {job_id}")
    return new_id
