# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P job queue implementation.

Provides the same interface as services/job_queue.py but uses libp2p.
Can be used as a drop-in replacement when P2P mode is enabled.

Architecture:
    Gateway → submit_job() → gossipsub /aipg/1/jobs/{model}
                                    ↓
    Workers ← pop_job() ← message dispatcher
                                    ↓
              claim_job() → gossipsub /aipg/1/claims
                                    ↓
              stream tokens → DIRECT STREAM to requester peer
                              (not gossipsub - more efficient)

Gossipsub is used for job/claim broadcast (one-to-many).
Direct streams are used for result streaming (one-to-one).
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
from .node import get_p2p_node, P2PMessage
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
_result_queues: dict[str, asyncio.Queue] = {}  # job_id -> queue of results
_dispatcher_task: asyncio.Task | None = None


async def _start_dispatcher() -> None:
    """Start the background message dispatcher."""
    global _dispatcher_task

    if _dispatcher_task is not None:
        return

    _dispatcher_task = asyncio.create_task(_message_dispatcher())
    logger.info("P2P message dispatcher started")


async def _message_dispatcher() -> None:
    """Background task that routes incoming P2P messages."""
    node = get_p2p_node()
    if not node:
        return

    config = get_p2p_config()
    cleanup_interval = 60  # seconds
    last_cleanup = time.time()

    while True:
        try:
            # Get next message from P2P node
            msg = await node.get_message(timeout=0.5)

            if msg:
                await _handle_message(msg)

            # Periodic cleanup of expired jobs
            now = time.time()
            if now - last_cleanup > cleanup_interval:
                _cleanup_expired()
                last_cleanup = now

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Dispatcher error: {e}")
            await asyncio.sleep(0.1)


async def _handle_message(msg: P2PMessage) -> None:
    """Route a message to the appropriate handler."""
    topic = msg.topic
    data = msg.data

    try:
        if "/jobs/" in topic:
            await _handle_job_message(topic, data, msg.from_peer)
        elif "/claims" in topic:
            await _handle_claim_message(data, msg.from_peer)
        elif "/results/" in topic:
            await _handle_result_message(topic, data)
        else:
            logger.debug(f"Unknown topic: {topic}")
    except Exception as e:
        logger.error(f"Error handling message on {topic}: {e}")


async def _handle_job_message(topic: str, data: bytes, from_peer: str) -> None:
    """Handle an incoming job broadcast."""
    try:
        job = JobRequest.from_json(data.decode())

        # Skip expired jobs
        if job.is_expired():
            logger.debug(f"Skipping expired job {job.id[:8]}")
            return

        # Skip already-claimed jobs
        if job.id in _claimed_jobs:
            claimer = _claimed_jobs[job.id].worker_id
            node = get_p2p_node()
            if node and claimer != node.peer_id:
                logger.debug(f"Skipping claimed job {job.id[:8]}")
                return

        # Store pending job
        _pending_jobs[job.id] = PendingJob(request=job)

        # Add to model queue
        queue = _job_queues.setdefault(job.model, asyncio.Queue())
        await queue.put({
            "job_id": job.id,
            "payload": job.payload,
            "models": [job.model],
            "job_request": job,  # Include full request for claim resolution
        })

        logger.info(f"Queued job {job.id[:8]} for model {job.model}")

    except Exception as e:
        logger.error(f"Error handling job message: {e}")


async def _handle_claim_message(data: bytes, from_peer: str) -> None:
    """Handle an incoming claim broadcast."""
    try:
        claim = JobClaim.from_json(data.decode())

        # Record the claim (first one wins)
        existing = _claimed_jobs.get(claim.job_id)
        if not existing or claim.timestamp < existing.timestamp:
            _claimed_jobs[claim.job_id] = claim
            logger.debug(f"Recorded claim: {claim.worker_id[:8]} -> {claim.job_id[:8]}")

            # Track the worker
            node = get_p2p_node()
            if node:
                node.add_known_worker(claim.worker_id)

    except Exception as e:
        logger.error(f"Error handling claim message: {e}")


async def _handle_result_message(topic: str, data: bytes) -> None:
    """Handle an incoming result message."""
    try:
        result = JobResult.from_json(data.decode())

        # Route to the job's result queue
        queue = _result_queues.get(result.job_id)
        if queue:
            await queue.put(result)

    except Exception as e:
        logger.error(f"Error handling result message: {e}")


def _cleanup_expired() -> None:
    """Remove expired jobs and claims from local state."""
    config = get_p2p_config()
    now = time.time()
    ttl = config.job_ttl_seconds

    # Clean pending jobs
    expired_jobs = [
        job_id for job_id, pending in _pending_jobs.items()
        if now - pending.received_at > ttl
    ]
    for job_id in expired_jobs:
        del _pending_jobs[job_id]

    # Clean claims
    expired_claims = [
        job_id for job_id, claim in _claimed_jobs.items()
        if now - claim.timestamp > ttl * 2  # Keep claims longer
    ]
    for job_id in expired_claims:
        del _claimed_jobs[job_id]

    if expired_jobs or expired_claims:
        logger.debug(f"Cleaned up {len(expired_jobs)} jobs, {len(expired_claims)} claims")


# ── Gateway API ──

async def submit_job(job_id: str, payload: dict, models: list[str]) -> str:
    """Submit a job for processing via P2P.

    Broadcasts the job to gossipsub topics for each specified model.
    Workers will open a direct stream to us to send results.
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    config = get_p2p_config()

    # Ensure dispatcher is running
    await _start_dispatcher()

    # Register stream inbox BEFORE broadcasting so it's ready when worker connects
    node.register_job_stream(job_id)

    # Create job request with our peer ID so worker knows where to stream results
    job = JobRequest(
        id=job_id,
        model=models[0] if models else "unknown",
        payload=payload,
        max_cost=0,  # Free tier for now
        user_pubkey="",  # TODO: from auth
        signature=uuid4().hex + uuid4().hex,  # Random seed for claim resolution
        requester_peer_id=node.peer_id,  # Worker streams results to this peer
        ttl=config.job_ttl_seconds,
    )

    # Broadcast to all model topics
    for model in models:
        job.model = model
        topic = job_topic(model)
        await node.publish(topic, job.to_json().encode())

    logger.info(f"Submitted job {job_id[:8]} to P2P for models: {models}")
    return job_id


async def pop_job(worker_id: str, timeout_ms: int = 5000) -> dict | None:
    """Wait for the next job from the P2P network.

    Called by workers to receive jobs. Blocks until a job arrives or timeout.
    """
    # Ensure dispatcher is running
    await _start_dispatcher()

    timeout_sec = timeout_ms / 1000.0
    end_time = time.time() + timeout_sec

    # Check all model queues
    while time.time() < end_time:
        for model, queue in list(_job_queues.items()):
            try:
                job = queue.get_nowait()

                # Verify we should claim this job
                job_request = job.get("job_request")
                if job_request:
                    node = get_p2p_node()
                    if node:
                        known = node.get_known_workers()
                        if not should_claim(job_request, node.peer_id, known):
                            logger.debug(f"Not our turn for job {job['job_id'][:8]}")
                            continue

                return job
            except asyncio.QueueEmpty:
                continue

        await asyncio.sleep(0.1)

    return None


async def claim_job(job_id: str, worker_id: str) -> bool:
    """Claim a job for this worker.

    Broadcasts the claim so other workers skip it.
    Returns False if already claimed by another worker.
    """
    node = get_p2p_node()
    if not node:
        return False

    # Check if already claimed by someone else
    if job_id in _claimed_jobs:
        existing = _claimed_jobs[job_id]
        if existing.worker_id != worker_id:
            logger.debug(f"Job {job_id[:8]} already claimed by {existing.worker_id[:8]}")
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

    topic = claims_topic()
    await node.subscribe(topic)  # Ensure subscribed
    await node.publish(topic, claim.to_json().encode())

    logger.info(f"Claimed job {job_id[:8]}")
    return True


async def ack_job(message_id: str) -> None:
    """Acknowledge a completed job. Cleans up local state."""
    _pending_jobs.pop(message_id, None)
    _claimed_jobs.pop(message_id, None)
    _result_queues.pop(message_id, None)


async def requeue_job(
    job_id: str, payload: dict, models: list[str], stream_id: str = None
) -> str:
    """Requeue a failed job."""
    _claimed_jobs.pop(job_id, None)
    return await submit_job(job_id, payload, models)


async def is_claimed(job_id: str) -> bool:
    """Check if a job has been claimed."""
    return job_id in _claimed_jobs


def get_claim(job_id: str) -> JobClaim | None:
    """Get the claim for a job."""
    return _claimed_jobs.get(job_id)


# ── Worker API ──

async def register_worker(models: list[str]) -> None:
    """Register this node as a worker for the given models.

    Subscribes to job topics and the claims topic.
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    # Ensure dispatcher is running
    await _start_dispatcher()

    # Subscribe to job topics
    for model in models:
        topic = job_topic(model)
        await node.subscribe(topic)
        _job_queues.setdefault(model, asyncio.Queue())
        logger.info(f"Listening for jobs on {topic}")

    # Subscribe to claims
    await node.subscribe(claims_topic())

    logger.info(f"Registered as worker for models: {models}")


async def stream_result(
    job_id: str, worker_id: str = ""
) -> AsyncGenerator[JobResult, None]:
    """Receive streaming results for a job via direct stream.

    Workers open a direct libp2p stream to us and send result messages.
    Much more efficient than gossipsub for high-frequency token streaming.
    """
    node = get_p2p_node()
    if not node:
        raise RuntimeError("P2P node not initialized")

    config = get_p2p_config()
    deadline = time.time() + config.job_ttl_seconds

    try:
        while time.time() < deadline:
            try:
                # Get next message from direct stream
                msg = await node.get_stream_message(job_id, timeout=1.0)

                if msg is None:
                    continue

                if msg.is_done:
                    # Stream closed by worker
                    logger.debug(f"Stream closed for job {job_id[:8]}")
                    break

                # Parse result from stream message
                try:
                    result = JobResult.from_json(msg.data.decode())
                    yield result

                    if result.type in ("done", "error"):
                        break
                except Exception as e:
                    logger.error(f"Error parsing stream message: {e}")
                    continue

            except asyncio.TimeoutError:
                continue
    finally:
        # Cleanup
        node.cleanup_job_stream(job_id)


async def publish_token(job_id: str, worker_id: str, text: str, index: int) -> None:
    """Publish a token result."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.token_msg(job_id, worker_id, text, index)
    topic = results_topic(job_id)
    await node.publish(topic, result.to_json().encode())


async def publish_done(
    job_id: str, worker_id: str, full_text: str, token_count: int
) -> None:
    """Publish a completion result."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.done_msg(job_id, worker_id, full_text, token_count, "")
    topic = results_topic(job_id)
    await node.publish(topic, result.to_json().encode())


async def publish_error(job_id: str, worker_id: str, message: str) -> None:
    """Publish an error result."""
    node = get_p2p_node()
    if not node:
        return

    result = JobResult.error_msg(job_id, worker_id, message)
    topic = results_topic(job_id)
    await node.publish(topic, result.to_json().encode())
