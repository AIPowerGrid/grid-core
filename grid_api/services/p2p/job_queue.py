# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P job queue implementation.

This module provides the same interface as services/job_queue.py but uses
libp2p gossipsub instead of Redis Streams. It can be used as a drop-in
replacement when P2P mode is enabled.

Architecture:
    - Jobs are broadcast to model-specific gossipsub topics
    - Workers subscribe to topics for models they support
    - Claims are broadcast to prevent double-processing
    - Results are streamed back via job-specific topics

Usage:
    # Gateway submits a job
    await submit_job(job_id, payload, models)

    # Worker listens for jobs
    async for job in listen_for_jobs(["llama3.2:3b", "mistral:7b"]):
        if should_process(job):
            await claim_job(job.id, worker_id)
            # ... process job ...
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .config import get_p2p_config
from .node import get_p2p_node
from .protocol import JobRequest, JobClaim, JobResult, should_claim
from .topics import job_topic, claims_topic, results_topic

logger = logging.getLogger("grid_api.p2p.job_queue")


@dataclass
class PendingJob:
    """A job waiting to be processed."""

    request: JobRequest
    received_at: float = field(default_factory=time.time)
    claimed_by: str | None = None


# Local state
_pending_jobs: dict[str, PendingJob] = {}
_claimed_jobs: dict[str, JobClaim] = {}
_job_queues: dict[str, asyncio.Queue] = {}  # model -> queue of jobs


async def submit_job(job_id: str, payload: dict, models: list[str]) -> str:
    """Submit a job for processing via P2P.

    This broadcasts the job to gossipsub topics for each specified model.
    Workers subscribed to those topics will receive the job.

    Args:
        job_id: Unique job identifier
        payload: The job payload (messages, params, etc.)
        models: List of acceptable models for this job

    Returns:
        The job_id (for compatibility with Redis interface)
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    config = get_p2p_config()

    # Create job request
    # Note: In production, user_pubkey and signature would come from auth
    job = JobRequest(
        id=job_id,
        model=models[0] if models else "unknown",
        payload=payload,
        max_cost=0,  # Free tier for now
        user_pubkey="",  # TODO: from auth
        signature=uuid4().hex + uuid4().hex,  # Random seed for claim resolution
        ttl=config.job_ttl_seconds,
    )

    # Broadcast to all model topics
    for model in models:
        job.model = model
        await node.publish_job(job)

    logger.info(f"Submitted job {job_id} to P2P network for models: {models}")
    return job_id


async def pop_job(worker_id: str, timeout_ms: int = 5000) -> dict | None:
    """Wait for the next job from the P2P network.

    This is called by workers to receive jobs. It blocks until a job
    arrives or timeout occurs.

    Note: Unlike Redis XREADGROUP, this doesn't guarantee exactly-once
    delivery. Workers should use claim_job() to coordinate.

    Args:
        worker_id: The worker's peer ID
        timeout_ms: How long to wait (milliseconds)

    Returns:
        Job dict with keys: job_id, payload, models, or None on timeout
    """
    # Find a queue with pending jobs
    timeout_sec = timeout_ms / 1000.0

    # Check all model queues we're subscribed to
    for model, queue in _job_queues.items():
        try:
            job = await asyncio.wait_for(queue.get(), timeout=timeout_sec)
            return job
        except asyncio.TimeoutError:
            continue

    return None


async def claim_job(job_id: str, worker_id: str) -> bool:
    """Claim a job for this worker.

    Broadcasts the claim to the network so other workers don't process it.
    Returns False if someone else already claimed it.

    Args:
        job_id: The job ID to claim
        worker_id: This worker's peer ID

    Returns:
        True if claim succeeded, False if already claimed by another worker
    """
    node = get_p2p_node()
    if not node:
        return False

    # Check if already claimed
    if job_id in _claimed_jobs:
        existing = _claimed_jobs[job_id]
        if existing.worker_id != worker_id:
            logger.debug(f"Job {job_id} already claimed by {existing.worker_id}")
            return False
        return True  # We already claimed it

    # Create and broadcast claim
    claim = JobClaim(
        job_id=job_id,
        worker_id=worker_id,
        worker_pubkey="",  # TODO: from wallet
        price=0,
        signature="",  # TODO: sign
    )

    _claimed_jobs[job_id] = claim
    await node.publish_claim(claim)

    logger.info(f"Claimed job {job_id} for worker {worker_id}")
    return True


async def ack_job(message_id: str) -> None:
    """Acknowledge a completed job.

    In the P2P model, this just cleans up local state.
    The message_id is actually the job_id.
    """
    # Clean up local state
    _pending_jobs.pop(message_id, None)
    _claimed_jobs.pop(message_id, None)


async def requeue_job(
    job_id: str, payload: dict, models: list[str], stream_id: str = None
) -> str:
    """Requeue a failed job.

    In P2P mode, we simply resubmit the job with the same ID.
    Other workers will see it and can claim it.
    """
    # Clean up old claim
    _claimed_jobs.pop(job_id, None)

    # Resubmit
    return await submit_job(job_id, payload, models)


async def is_claimed(job_id: str) -> bool:
    """Check if a job has been claimed."""
    return job_id in _claimed_jobs


def get_claim(job_id: str) -> JobClaim | None:
    """Get the claim for a job."""
    return _claimed_jobs.get(job_id)


# ── Worker-side functions ──


async def register_worker(models: list[str]) -> None:
    """Register this node as a worker for the given models.

    This subscribes to the appropriate job topics and sets up
    message handling.
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    async def handle_job(topic: str, data: bytes) -> None:
        """Handle incoming job messages."""
        try:
            job = JobRequest.from_json(data.decode())

            # Skip expired jobs
            if job.is_expired():
                logger.debug(f"Skipping expired job {job.id}")
                return

            # Skip already-claimed jobs
            if job.id in _claimed_jobs:
                claimer = _claimed_jobs[job.id].worker_id
                if claimer != node.peer_id:
                    logger.debug(f"Skipping claimed job {job.id}")
                    return

            # Check if we should claim this job
            known_workers = node.get_known_workers()
            if not should_claim(job, node.peer_id, known_workers):
                logger.debug(f"Not our turn to claim job {job.id}")
                return

            # Add to queue for processing
            queue = _job_queues.setdefault(job.model, asyncio.Queue())
            await queue.put({
                "job_id": job.id,
                "payload": job.payload,
                "models": [job.model],
            })

            logger.info(f"Queued job {job.id} for model {job.model}")

        except Exception as e:
            logger.error(f"Error handling job message: {e}")

    async def handle_claim(topic: str, data: bytes) -> None:
        """Handle incoming claim messages."""
        try:
            claim = JobClaim.from_json(data.decode())

            # Record the claim
            existing = _claimed_jobs.get(claim.job_id)
            if not existing or claim.timestamp < existing.timestamp:
                _claimed_jobs[claim.job_id] = claim
                logger.debug(f"Recorded claim: {claim.worker_id} -> {claim.job_id}")

                # Add worker to known set
                node.add_known_worker(claim.worker_id)

        except Exception as e:
            logger.error(f"Error handling claim message: {e}")

    # Subscribe to job topics for each model
    await node.subscribe_to_jobs(models, handle_job)

    # Subscribe to global claims topic
    await node.subscribe_to_claims(handle_claim)

    logger.info(f"Registered as worker for models: {models}")


async def stream_result(
    job_id: str, worker_id: str
) -> AsyncGenerator[JobResult, None]:
    """Subscribe to results for a job.

    Used by gateways to receive streaming results from workers.

    Args:
        job_id: The job to watch
        worker_id: Expected worker ID (optional validation)

    Yields:
        JobResult messages (tokens, done, or error)
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    result_queue: asyncio.Queue[JobResult] = asyncio.Queue()

    async def handle_result(topic: str, data: bytes) -> None:
        result = JobResult.from_json(data.decode())
        await result_queue.put(result)

    topic = results_topic(job_id)
    await node.subscribe(topic, handle_result)

    try:
        config = get_p2p_config()
        deadline = time.time() + config.job_ttl_seconds

        while time.time() < deadline:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=1.0)
                yield result

                if result.type in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                continue

    finally:
        await node.unsubscribe(topic)


async def publish_token(job_id: str, worker_id: str, text: str, index: int) -> None:
    """Publish a token result to the job's result topic."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.token_msg(job_id, worker_id, text, index)
    await node.publish_result(result)


async def publish_done(
    job_id: str, worker_id: str, full_text: str, token_count: int
) -> None:
    """Publish a completion result."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.done_msg(job_id, worker_id, full_text, token_count, "")
    await node.publish_result(result)


async def publish_error(job_id: str, worker_id: str, message: str) -> None:
    """Publish an error result."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.error_msg(job_id, worker_id, message)
    await node.publish_result(result)
