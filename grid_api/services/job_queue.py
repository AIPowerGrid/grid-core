# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job dispatch via Redis Streams with retry support.

Workers consume jobs from the `grid:jobs:text` stream using XREADGROUP.
The API submits jobs via XADD. Failed/abandoned jobs are requeued
automatically via claim_stale_jobs().
"""

import json
import logging

from ..redis_client import CONSUMER_GROUP, STREAM_KEY, get_redis

logger = logging.getLogger("grid_api.job_queue")

# Jobs pending longer than this are considered abandoned and can be reclaimed
STALE_JOB_MS = 300_000  # 5 minutes


async def submit_job(job_id: str, payload: dict, models: list[str]) -> str:
    """Add a text generation job to the Redis Stream."""
    r = get_redis()
    data = {
        "job_id": job_id,
        "payload": json.dumps(payload),
        "models": json.dumps(models),
    }
    return await r.xadd(STREAM_KEY, data)


async def pop_job(worker_id: str, timeout_ms: int = 5000) -> dict | None:
    """Block-wait for the next job from the stream.

    Uses XREADGROUP so each job goes to exactly one worker.
    Returns None on timeout (no jobs available).
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

    return {
        "stream_id": message_id,
        "job_id": fields["job_id"],
        "payload": json.loads(fields["payload"]),
        "models": json.loads(fields["models"]),
    }


async def ack_job(message_id: str):
    """Acknowledge a completed job so it's removed from the pending list."""
    r = get_redis()
    await r.xack(STREAM_KEY, CONSUMER_GROUP, message_id)


async def requeue_job(job_id: str, payload: dict, models: list[str], stream_id: str = None):
    """Requeue a failed job back into the stream.

    Called when a worker disconnects mid-generation. The original stream
    message is acked (if stream_id provided) and a new one is added.
    """
    r = get_redis()
    if stream_id:
        await r.xack(STREAM_KEY, CONSUMER_GROUP, stream_id)
    new_id = await submit_job(job_id, payload, models)
    logger.info(f"Requeued job {job_id} as {new_id}")
    return new_id


async def claim_stale_jobs() -> int:
    """Reclaim jobs stuck in pending state for longer than STALE_JOB_MS.

    These are jobs a worker popped but never acked (worker crashed).
    We re-add them to the stream for another worker to pick up.
    Returns the number of jobs reclaimed.
    """
    r = get_redis()
    # XAUTOCLAIM: grab pending messages older than STALE_JOB_MS
    try:
        result = await r.xautoclaim(
            STREAM_KEY, CONSUMER_GROUP, "reclaimer", min_idle_time=STALE_JOB_MS, start_id="0-0", count=10,
        )
        # result = (next_start_id, [(msg_id, fields), ...], [deleted_ids])
        if not result or not result[1]:
            return 0

        reclaimed = 0
        for msg_id, fields in result[1]:
            job_id = fields.get("job_id", "unknown")
            logger.warning(f"Reclaiming stale job {job_id} (msg {msg_id})")
            # Ack the old message and requeue
            await r.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)
            await submit_job(
                job_id,
                json.loads(fields.get("payload", "{}")),
                json.loads(fields.get("models", "[]")),
            )
            reclaimed += 1
        return reclaimed
    except Exception as e:
        logger.error(f"Error claiming stale jobs: {e}")
        return 0
