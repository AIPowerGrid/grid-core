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

# Jobs pending longer than this are considered abandoned and can be reclaimed.
# Per-stream: a media (image/video) job can legitimately run for minutes —
# VIDEO_TIMEOUT is 600s and a cold model load adds more — so its stale window
# MUST exceed the job's own allowed runtime, else the reclaimer yanks a job that
# is still rendering, re-dispatches it, and the client times out on doubled work.
# Text jobs stream tokens and complete fast, so they keep the tight window.
STALE_JOB_MS = 300_000        # text: 5 minutes
STALE_JOB_MS_MEDIA = 900_000  # media: 15 minutes (> VIDEO_TIMEOUT incl. cold start)


def _stale_ms_for(stream: str) -> int:
    return STALE_JOB_MS_MEDIA if stream == MEDIA_STREAM_KEY else STALE_JOB_MS

# A job can be requeued (bounced between workers that don't serve its model)
# at most this many times before we give up and fault it. With instant
# requeues, a job for a model NO worker serves hits this cap in milliseconds
# and fails cleanly, instead of hanging the client until the 300s timeout.
# Set comfortably above the realistic number of model-mismatched workers a
# job might bounce through in a healthy heterogeneous pool.
MAX_REQUEUE = 25

# Cap the job streams so they don't grow without bound. XACK removes a job from
# the consumer group's pending list but NOT from the stream itself, so without
# trimming the stream accumulates every job's full payload (prompts included)
# forever — a slow memory leak + data-retention issue. Approximate trimming (~)
# is cheap and keeps well above the cap, so in-flight/recent jobs (a handful,
# near the head) are never trimmed. Generous default; override via env.
import os
MAX_STREAM_LEN = int(os.getenv("GRID_JOB_STREAM_MAXLEN", "10000") or 10000)

# Soft worker-affinity: a job may name a preferred_worker (ownership-gated at
# submit time). A non-preferred worker that pops it releases it back so the
# preferred worker can claim it — UNLESS the preferred worker is offline or the
# job has already bounced this many times, in which case it runs wherever it
# landed (affinity is a preference, never a stall). Kept low: in a small pool the
# preferred worker reclaims within a couple of XREADGROUP cycles.
# Validator probes may also set hard_target_worker. That is not a preference:
# a non-target worker must never execute the job.
MAX_AFFINITY_BOUNCE = 10


def _stream_for(job_type: str) -> str:
    return STREAM_KEY if job_type == "text" else MEDIA_STREAM_KEY


async def submit_job(
    job_id: str,
    payload: dict,
    models: list[str],
    requeue_count: int = 0,
    job_type: str = "text",
    preferred_worker: str = "",
    hard_target_worker: str = "",
    affinity_passes: int = 0,
    progress_token: str = "",
) -> str:
    """Add a generation job to its type's Redis Stream.

    `preferred_worker` (a worker NAME, ownership-checked by the caller) lets the
    job express soft affinity — see MAX_AFFINITY_BOUNCE. Empty = no preference.
    `hard_target_worker` is used by validator probes and forbids fallback.
    `progress_token` (a client-chosen id) lets the worker's live % be polled at
    GET /v1/progress/{token} while the job runs.
    """
    r = get_redis()
    data = {
        "job_id": job_id,
        "job_type": job_type,
        "payload": json.dumps(payload),
        "models": json.dumps(models),
        "requeue_count": str(requeue_count),
        "preferred_worker": preferred_worker or "",
        "hard_target_worker": hard_target_worker or "",
        "affinity_passes": str(affinity_passes),
        "progress_token": progress_token or "",
    }
    return await r.xadd(_stream_for(job_type), data, maxlen=MAX_STREAM_LEN, approximate=True)


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
        "preferred_worker": fields.get("preferred_worker", ""),
        "hard_target_worker": fields.get("hard_target_worker", ""),
        "affinity_passes": int(fields.get("affinity_passes", 0)),
        "progress_token": fields.get("progress_token", ""),
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
        # Preserve affinity across a model-mismatch bounce, else a preferred
        # worker loses its claim the moment a mismatched worker touches the job.
        preferred_worker=job.get("preferred_worker", ""),
        hard_target_worker=job.get("hard_target_worker", ""),
        affinity_passes=job.get("affinity_passes", 0),
        progress_token=job.get("progress_token", ""),
    )
    return True


async def bounce_for_affinity(job: dict) -> bool:
    """Release a job a non-preferred worker popped so its preferred worker can
    claim it. Acks the current delivery and re-adds the job with the affinity
    pass counter incremented.

    Returns True if bounced, False if the bounce limit was hit (caller should
    run the job locally rather than stall it — affinity is a preference).
    """
    r = get_redis()
    job_type = job.get("job_type", "text")
    passes = job.get("affinity_passes", 0)

    # Ack the current delivery either way so it leaves this worker's PEL.
    await r.xack(job.get("stream", _stream_for(job_type)), CONSUMER_GROUP, job["stream_id"])

    if passes >= MAX_AFFINITY_BOUNCE:
        logger.info(
            f"Job {job['job_id']} hit affinity bounce limit ({MAX_AFFINITY_BOUNCE}) "
            f"for preferred '{job.get('preferred_worker')}' — running on available worker."
        )
        return False

    await submit_job(
        job["job_id"], job["payload"], job["models"],
        requeue_count=job.get("requeue_count", 0), job_type=job_type,
        preferred_worker=job.get("preferred_worker", ""),
        hard_target_worker=job.get("hard_target_worker", ""),
        affinity_passes=passes + 1,
        progress_token=job.get("progress_token", ""),
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
    requeue_count: int = 0,
    preferred_worker: str = "",
    hard_target_worker: str = "",
    affinity_passes: int = 0,
):
    """Requeue a failed job back into the stream, carrying + capping the retry
    count. Returns the new stream id, or None if the job has hit MAX_REQUEUE and
    must be dead-lettered.

    Without a cap a "poison" job (one that fails on every attempt — e.g. a
    request the backend can't serve, or a transient that recurs) loops forever:
    fail → requeue → redeliver → fail, striking and evicting every worker that
    touches it (the 2026-06-16 gpt-oss "0 tokens" eviction cascade). Capping it
    turns an infinite loop into a clean per-client failure."""
    r = get_redis()
    if stream_id:
        await r.xack(stream or _stream_for(job_type), CONSUMER_GROUP, stream_id)
    # Self-contained retry counter keyed by job_id — works regardless of whether
    # the caller threads requeue_count, so a poison job is capped even on the
    # failure path. Cleared by TTL (and the job_id is unique per request).
    attempts = await r.incr(f"grid:requeue:{job_id}")
    await r.expire(f"grid:requeue:{job_id}", 600)
    if attempts > MAX_REQUEUE:
        logger.error(
            f"Job {job_id} hit MAX_REQUEUE ({MAX_REQUEUE}) after repeated failures "
            f"— dead-lettering instead of requeuing"
        )
        return None
    new_id = await submit_job(
        job_id, payload, models,
        requeue_count=requeue_count + 1, job_type=job_type,
        preferred_worker=preferred_worker,
        hard_target_worker=hard_target_worker,
        affinity_passes=affinity_passes,
    )
    logger.info(f"Requeued job {job_id} as {new_id} (attempt {attempts}/{MAX_REQUEUE})")
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
                stream, CONSUMER_GROUP, "reclaimer", min_idle_time=_stale_ms_for(stream), start_id="0-0", count=10,
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
                    preferred_worker=fields.get("preferred_worker", ""),
                    hard_target_worker=fields.get("hard_target_worker", ""),
                    affinity_passes=int(fields.get("affinity_passes", 0)),
                    progress_token=fields.get("progress_token", ""),
                )
                reclaimed += 1
        except Exception as e:
            logger.error(f"Error claiming stale jobs from {stream}: {e}")
    return reclaimed
