# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decentralized job queue via Waku relay network.

Jobs are broadcast to all nodes/workers subscribed to the topic.
First worker to claim a job wins. Claims are also broadcast to
prevent double-processing.

This replaces Redis Streams for job distribution while keeping
Redis for local state (connected workers, token streaming).
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional
from uuid import uuid4

logger = logging.getLogger("grid_api.waku_queue")

# Waku content topics (namespaced for AIPG)
TOPIC_JOBS = "/aipowergrid/1/jobs/json"
TOPIC_CLAIMS = "/aipowergrid/1/claims/json"
TOPIC_RESULTS = "/aipowergrid/1/results/json"

# Waku node configuration
WAKU_REST_URL = os.getenv("WAKU_REST_URL", "http://127.0.0.1:8645")
WAKU_PEER = os.getenv("WAKU_PEER", "")  # Bootstrap peer multiaddr

# Job TTL - if not claimed within this time, can be resubmitted
JOB_TTL_SECONDS = 60


@dataclass
class Job:
    id: str
    payload: dict
    models: list[str]
    submitted_at: float
    submitted_by: str  # Node ID that submitted this job

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "payload": self.payload,
            "models": self.models,
            "submitted_at": self.submitted_at,
            "submitted_by": self.submitted_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            id=data["id"],
            payload=data["payload"],
            models=data["models"],
            submitted_at=data["submitted_at"],
            submitted_by=data["submitted_by"],
        )


@dataclass
class Claim:
    job_id: str
    worker_id: str
    claimed_at: float

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "claimed_at": self.claimed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        return cls(
            job_id=data["job_id"],
            worker_id=data["worker_id"],
            claimed_at=data["claimed_at"],
        )


class WakuJobQueue:
    """
    Decentralized job queue using Waku relay.

    Architecture:
    - API nodes publish jobs to TOPIC_JOBS
    - Workers subscribe to TOPIC_JOBS, filter by models they support
    - Worker claims job by publishing to TOPIC_CLAIMS
    - All nodes see claims, mark job as taken
    - Results published to TOPIC_RESULTS (or streamed via WebSocket)

    Consistency:
    - Jobs have UUIDs - duplicate broadcasts are idempotent
    - Claims are timestamped - earliest claim wins on conflict
    - Unclaimed jobs can be resubmitted after TTL
    """

    def __init__(self, node_id: str = None):
        self.node_id = node_id or str(uuid4())[:8]
        self._pending_jobs: dict[str, Job] = {}  # job_id -> Job
        self._claimed_jobs: dict[str, Claim] = {}  # job_id -> Claim
        self._job_handlers: list[Callable] = []
        self._running = False
        self._ws = None

    async def start(self):
        """Connect to Waku and start listening."""
        logger.info(f"[WAKU] Starting node {self.node_id}...")

        # For production: use waku-py or nwaku bindings
        # For now: use REST API to local nwaku node
        try:
            import aiohttp
            self._session = aiohttp.ClientSession()

            # Subscribe to topics
            await self._subscribe(TOPIC_JOBS)
            await self._subscribe(TOPIC_CLAIMS)

            # Start message polling (REST) or WebSocket listener
            self._running = True
            asyncio.create_task(self._poll_messages())

            logger.info(f"[WAKU] Node {self.node_id} connected")

        except Exception as e:
            logger.error(f"[WAKU] Failed to start: {e}")
            raise

    async def stop(self):
        """Disconnect from Waku."""
        self._running = False
        if self._session:
            await self._session.close()
        logger.info(f"[WAKU] Node {self.node_id} stopped")

    async def submit_job(self, job_id: str, payload: dict, models: list[str]) -> Job:
        """
        Submit a job to the network.

        All subscribed nodes and workers will receive this.
        """
        job = Job(
            id=job_id,
            payload=payload,
            models=models,
            submitted_at=time.time(),
            submitted_by=self.node_id,
        )

        await self._publish(TOPIC_JOBS, job.to_dict())
        self._pending_jobs[job_id] = job

        logger.info(f"[WAKU] Published job {job_id} for models {models}")
        return job

    async def claim_job(self, job_id: str, worker_id: str) -> bool:
        """
        Claim a job for processing.

        Returns True if claim was successful (no prior claim).
        Broadcasts claim to network so other workers skip this job.
        """
        # Check if already claimed
        if job_id in self._claimed_jobs:
            existing = self._claimed_jobs[job_id]
            if existing.worker_id != worker_id:
                logger.debug(f"[WAKU] Job {job_id} already claimed by {existing.worker_id}")
                return False
            return True  # We already claimed it

        claim = Claim(
            job_id=job_id,
            worker_id=worker_id,
            claimed_at=time.time(),
        )

        # Optimistically mark as claimed locally
        self._claimed_jobs[job_id] = claim

        # Broadcast claim to network
        await self._publish(TOPIC_CLAIMS, claim.to_dict())

        logger.info(f"[WAKU] Worker {worker_id} claimed job {job_id}")
        return True

    def on_job(self, handler: Callable[[Job], None]):
        """Register a handler for incoming jobs."""
        self._job_handlers.append(handler)

    def is_claimed(self, job_id: str) -> bool:
        """Check if a job has been claimed."""
        return job_id in self._claimed_jobs

    def get_claim(self, job_id: str) -> Optional[Claim]:
        """Get claim info for a job."""
        return self._claimed_jobs.get(job_id)

    # ── Internal Methods ──

    async def _subscribe(self, topic: str):
        """Subscribe to a Waku content topic."""
        url = f"{WAKU_REST_URL}/relay/v1/subscriptions"
        async with self._session.post(url, json=[topic]) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to subscribe to {topic}: {await resp.text()}")
        logger.debug(f"[WAKU] Subscribed to {topic}")

    async def _publish(self, topic: str, data: dict):
        """Publish a message to a Waku content topic."""
        import base64

        payload = base64.b64encode(json.dumps(data).encode()).decode()

        message = {
            "payload": payload,
            "contentTopic": topic,
            "timestamp": int(time.time() * 1e9),  # nanoseconds
        }

        url = f"{WAKU_REST_URL}/relay/v1/messages"
        async with self._session.post(url, json=message) as resp:
            if resp.status not in (200, 201):
                raise Exception(f"Failed to publish: {await resp.text()}")

    async def _poll_messages(self):
        """Poll for new messages (REST API approach)."""
        import base64

        while self._running:
            try:
                for topic in [TOPIC_JOBS, TOPIC_CLAIMS]:
                    url = f"{WAKU_REST_URL}/relay/v1/messages/{topic}"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            messages = await resp.json()
                            for msg in messages:
                                await self._handle_message(topic, msg)

                await asyncio.sleep(0.1)  # 100ms polling interval

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WAKU] Poll error: {e}")
                await asyncio.sleep(1)

    async def _handle_message(self, topic: str, msg: dict):
        """Process an incoming Waku message."""
        import base64

        try:
            payload = json.loads(base64.b64decode(msg["payload"]).decode())

            if topic == TOPIC_JOBS:
                job = Job.from_dict(payload)

                # Skip if we submitted this job
                if job.submitted_by == self.node_id:
                    return

                # Skip if already claimed
                if job.id in self._claimed_jobs:
                    return

                # Store and notify handlers
                self._pending_jobs[job.id] = job
                for handler in self._job_handlers:
                    try:
                        await handler(job) if asyncio.iscoroutinefunction(handler) else handler(job)
                    except Exception as e:
                        logger.error(f"[WAKU] Job handler error: {e}")

            elif topic == TOPIC_CLAIMS:
                claim = Claim.from_dict(payload)

                # If we don't have this claim, or this one is earlier, use it
                existing = self._claimed_jobs.get(claim.job_id)
                if not existing or claim.claimed_at < existing.claimed_at:
                    self._claimed_jobs[claim.job_id] = claim
                    logger.debug(f"[WAKU] Recorded claim: {claim.worker_id} -> {claim.job_id}")

        except Exception as e:
            logger.error(f"[WAKU] Message parse error: {e}")


# Singleton instance
_waku_queue: Optional[WakuJobQueue] = None


def get_waku_queue() -> WakuJobQueue:
    """Get the global Waku queue instance."""
    global _waku_queue
    if _waku_queue is None:
        _waku_queue = WakuJobQueue()
    return _waku_queue


async def init_waku():
    """Initialize Waku on startup."""
    if os.getenv("WAKU_ENABLED", "false").lower() == "true":
        queue = get_waku_queue()
        await queue.start()
        logger.info("[WAKU] Decentralized job queue enabled")


async def close_waku():
    """Shutdown Waku on exit."""
    global _waku_queue
    if _waku_queue:
        await _waku_queue.stop()
        _waku_queue = None
