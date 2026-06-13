# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job dispatch via Redis Streams with retry support.

Workers consume jobs from the `grid:jobs:text` stream using XREADGROUP.
The API submits jobs via XADD. Failed/abandoned jobs are requeued
automatically via claim_stale_jobs().
"""

import json
import logging

import redis.exceptions

from ..redis_client import CONSUMER_GROUP, MEDIA_STREAM_KEY, STREAM_KEY, get_redis

logger = logging.getLogger("grid_api.job_queue")

# Jobs pending longer than this are considered abandoned and can be reclaimed
STALE_JOB_MS = 300_000  # 5 minutes

# A job can be requeued (bounced between workers that don't serve its model)
# at most this many times before we give up and fault it. With instant
# requeues, a job for a model NO worker serves hits this cap in milliseconds
# and fails cleanly, instead of hanging the client until the 300s timeout.
# Set comfortably above the realistic number of model-mismatched workers a
# job might bounce through in a healthy heterogeneous pool.
MAX_REQUEUE = 25


def _stream_for(job_type: str) -> str:
    return STREAM_KEY if job_type == "text" else MEDIA_STREAM_KEY


async def submit_job(
    job_id: str,
    payload: dict,
    models: list[str],
    requeue_count: int = 0,
    job_type: str = "text",
) -> str:
    """Add a generation job to its type's Redis Stream."""
    r = get_redis()
    data = {
        "job_id": job_id,
        "job_type": job_type,
        "payload": json.dumps(payload),
        "models": json.dumps(models),
        "requeue_count": str(requeue_count),
    }
    return await r.xadd(_stream_for(job_type), data)


async def pop_job(worker_id: str, timeout_ms: int = 5000, job_types: list[str] | None = None) -> dict | None:
    """Block-wait for the next job from the stream(s) this worker serves.

    Uses XREADGROUP so each job goes to exactly one worker. A worker serving
    both text and media blocks on both streams in one call.
    Returns None on timeout (no jobs available).
    """
    r = get_redis()
    streams = sorted({_stream_for(t) for t in (job_types or ["text"])})
    try:
        results = await r.xreadgroup(
            CONSUMER_GROUP,
            worker_id,
            {s: ">" for s in streams},
            count=1,
            block=timeout_ms,
        )
    except redis.exceptions.TimeoutError:
        # The blocking read can race its own socket timeout when no job
        # arrives within the block window — that's just "no job", not an error.
        return None
    if not results:
        return None

    stream_name, messages = results[0]
    message_id, fields = messages[0]

    return {
        "stream_id": message_id,
        "stream": stream_name,
        "job_id": fields["job_id"],
        "job_type": fields.get("job_type", "text"),
        "payload": json.loads(fields["payload"]),
        "models": json.loads(fields["models"]),
        # Default 0 for jobs queued before this field existed.
        "requeue_count": int(fields.get("requeue_count", 0)),
    }


async def requeue_for_mismatch(job: dict) -> bool:
    """Requeue a job that landed on a worker that doesn't serve its model.

    The single shared stream + consumer group means XREADGROUP hands a job
    to a random worker regardless of which models it serves. Rather than
    discard a mismatched job (which silently strands the waiting client),
    we ack the current delivery and re-add it for another worker.

    Returns True if requeued, False if the bounce limit was hit (caller
    should fault the job and notify the client).
    """
    r = get_redis()
    count = job.get("requeue_count", 0)
    job_type = job.get("job_type", "text")

    # Ack the current delivery either way so it leaves this worker's PEL.
    await r.xack(job.get("stream", _stream_for(job_type)), CONSUMER_GROUP, job["stream_id"])

    if count >= MAX_REQUEUE:
        logger.warning(
            f"Job {job['job_id']} hit requeue limit ({MAX_REQUEUE}) for "
            f"models {job['models']} — no worker serves it; faulting."
        )
        return False

    await submit_job(
        job["job_id"], job["payload"], job["models"],
        requeue_count=count + 1, job_type=job_type,
    )
    return True


async def ack_job(message_id: str, stream: str = STREAM_KEY):
    """Acknowledge a completed job so it's removed from the pending list."""
    r = get_redis()
    await r.xack(stream, CONSUMER_GROUP, message_id)


async def requeue_job(
    job_id: str,
    payload: dict,
    models: list[str],
    stream_id: str = None,
    job_type: str = "text",
    stream: str | None = None,
):
    """Requeue a failed job back into the stream.

    Called when a worker disconnects mid-generation. The original stream
    message is acked (if stream_id provided) and a new one is added.
    """
    r = get_redis()
    if stream_id:
        await r.xack(stream or _stream_for(job_type), CONSUMER_GROUP, stream_id)
    new_id = await submit_job(job_id, payload, models, job_type=job_type)
    logger.info(f"Requeued job {job_id} as {new_id}")
    return new_id


async def claim_stale_jobs() -> int:
    """Reclaim jobs stuck in pending state for longer than STALE_JOB_MS.

    These are jobs a worker popped but never acked (worker crashed).
    We re-add them to the stream for another worker to pick up.
    Returns the number of jobs reclaimed.
    """
    r = get_redis()
    reclaimed = 0
    for stream in (STREAM_KEY, MEDIA_STREAM_KEY):
        # XAUTOCLAIM: grab pending messages older than STALE_JOB_MS
        try:
            result = await r.xautoclaim(
                stream, CONSUMER_GROUP, "reclaimer", min_idle_time=STALE_JOB_MS, start_id="0-0", count=10,
            )
            # result = (next_start_id, [(msg_id, fields), ...], [deleted_ids])
            if not result or not result[1]:
                continue

            for msg_id, fields in result[1]:
                job_id = fields.get("job_id", "unknown")
                logger.warning(f"Reclaiming stale job {job_id} (msg {msg_id}) from {stream}")
                # Ack the old message and requeue
                await r.xack(stream, CONSUMER_GROUP, msg_id)
                await submit_job(
                    job_id,
                    json.loads(fields.get("payload", "{}")),
                    json.loads(fields.get("models", "[]")),
                    job_type=fields.get("job_type", "text"),
                )
                reclaimed += 1
        except Exception as e:
            logger.error(f"Error claiming stale jobs from {stream}: {e}")
    return reclaimed
