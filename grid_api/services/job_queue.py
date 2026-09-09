# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job dispatch via Redis Streams with retry support.

Workers consume jobs from the `grid:jobs:text` stream using XREADGROUP.
The API submits jobs via XADD. Failed/abandoned jobs are requeued
automatically via claim_stale_jobs().
"""

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager

import redis.exceptions

from ..redis_client import CONSUMER_GROUP, MEDIA_STREAM_KEY, STREAM_KEY, get_redis
from ..safe_logging import error_type

logger = logging.getLogger("grid_api.job_queue")

# Jobs pending longer than this are considered abandoned and can be reclaimed.
# Per-stream: a media (image/video) job can legitimately run for minutes —
# VIDEO_TIMEOUT is 600s and a cold model load adds more — so its stale window
# MUST exceed the job's own allowed runtime, else the reclaimer yanks a job that
# is still rendering, re-dispatches it, and the client times out on doubled work.
# Text jobs stream tokens and complete fast, so they keep the tight window.
STALE_JOB_MS = 300_000  # text: 5 minutes
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
# An empty/failed generation has already reached a compatible worker and is a
# much stronger failure signal than a model-mismatch bounce. Permit two retries
# (three total backend attempts) so a transient can recover without hammering a
# sole worker or holding the client open through the generic 25-bounce budget.
MAX_GENERATION_REQUEUE = 2

# Cap the job streams so they don't grow without bound. XACK removes a job from
# the consumer group's pending list but NOT from the stream itself, so without
# trimming the stream accumulates every job's full payload (prompts included)
# forever — a slow memory leak + data-retention issue. Approximate trimming (~)
# is cheap and keeps well above the cap, so in-flight/recent jobs (a handful,
# near the head) are never trimmed. Generous default; override via env.
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


# Lua is indivisible with respect to a Core crash, but does not roll back on
# command errors. Validate/read first and XADD before XACK: a failed append must
# leave the original delivery pending. Copy Redis fields, not a caller's stale
# reconstruction, so progress/targeting and all retry counters survive recovery.
_HANDOFF = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending == 0 then return {'superseded', ARGV[2]} end
if ARGV[6] ~= '' and pending[1][2] ~= ARGV[6] then
    return {'superseded', ARGV[2]}
end
local source = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2])
if #source == 0 then return redis.error_reply('pending source missing') end
local fields = source[1][2]
local offsets = {}
for i = 1, #fields, 2 do offsets[fields[i]] = i + 1 end
local function read(name, default)
    if offsets[name] then return fields[offsets[name]] end
    return default
end
local function write(name, value)
    if offsets[name] then fields[offsets[name]] = tostring(value)
    else
        table.insert(fields, name)
        table.insert(fields, tostring(value))
        offsets[name] = #fields
    end
end
if read('job_id', '') ~= ARGV[3] then
    return redis.error_reply('pending job mismatch')
end
local count = tonumber(read('requeue_count', '0'))
local passes = tonumber(read('affinity_passes', '0'))
local failures = tonumber(read('generation_requeues', '0'))
local limit = tonumber(ARGV[5])
local maxlen = tonumber(ARGV[7])
if not count or not passes or not failures or not limit or not maxlen then
    return redis.error_reply('invalid retry counters')
end
local mode = ARGV[4]
if mode == 'affinity' then
    if passes >= limit then return {'run', ''} end
    write('affinity_passes', passes + 1)
elseif mode == 'mismatch' then
    if count >= limit then
        redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
        return {'closed', ''}
    end
    write('requeue_count', count + 1)
elseif mode == 'failure' then
    -- Carry forward an old release's counter during a rolling upgrade. New
    -- retries live on the message, so a long job cannot reset the cap by TTL.
    local legacy = tonumber(redis.call('GET', KEYS[2]) or '0')
    if not legacy then return redis.error_reply('invalid legacy retry counter') end
    failures = math.max(failures, legacy)
    if failures >= limit then
        redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
        return {'closed', ''}
    end
    write('generation_requeues', failures + 1)
    write('requeue_count', count + 1)
elseif mode ~= 'stale' then
    return redis.error_reply('invalid retry mode')
end
local replacement = redis.call('XADD', KEYS[1], 'MAXLEN', '~', maxlen, '*', unpack(fields))
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return {'requeued', replacement}
"""


async def _handoff(job: dict, mode: str, limit: int = MAX_REQUEUE) -> tuple[str, str]:
    """Transfer one pending delivery, or report that it already left this caller.

    Superseded is intentionally nonterminal: it must never trigger a refund or
    another dispatch. The returned old ID is an opaque truthy handle, not a
    promise that this invocation created a replacement.
    """
    if not job.get("stream_id"):
        raise ValueError("requeue requires a pending stream delivery")
    result = await get_redis().eval(
        _HANDOFF,
        2,
        job.get("stream") or _stream_for(job.get("job_type", "text")),
        f"grid:requeue:{job['job_id']}",
        CONSUMER_GROUP,
        job["stream_id"],
        job["job_id"],
        mode,
        limit,
        job.get("worker_id", ""),
        MAX_STREAM_LEN,
    )
    return result[0], result[1]


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
    `hard_target_worker` is used by validator probes and setup canaries and
    forbids fallback.
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
    status, _ = await _handoff(job, "mismatch")
    return status != "closed"


async def bounce_for_affinity(job: dict) -> bool:
    """Release a job a non-preferred worker popped so its preferred worker can
    claim it. Acks the current delivery and re-adds the job with the affinity
    pass counter incremented.

    Returns True if bounced, False if the bounce limit was hit (caller should
    run the job locally rather than stall it — affinity is a preference).
    """
    status, _ = await _handoff(job, "affinity", MAX_AFFINITY_BOUNCE)
    return status != "run"


async def ack_job(message_id: str, stream: str = STREAM_KEY):
    """Acknowledge a completed job so it's removed from the pending list."""
    r = get_redis()
    await r.xack(stream, CONSUMER_GROUP, message_id)


async def touch_job_claim(job: dict) -> None:
    """Reset a running job's pending-idle timer without changing ownership."""
    stream = job.get("stream") or _stream_for(job.get("job_type", "text"))
    await get_redis().xclaim(
        stream,
        CONSUMER_GROUP,
        job["worker_id"],
        min_idle_time=0,
        message_ids=[job["stream_id"]],
        justid=True,
    )


@asynccontextmanager
async def maintain_job_claim(job: dict, *, interval_seconds: float = 60.0):
    """Keep a legitimate long-running job below the stale-reclaim threshold."""

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await touch_job_claim(job)
            except Exception as exc:
                logger.error("Could not refresh claim for job %s: %s", job.get("job_id"), exc)

    task = asyncio.create_task(_heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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
    max_attempts: int = MAX_REQUEUE,
):
    """Requeue a failed job back into the stream, carrying + capping the retry
    count. Returns a truthy delivery handle (new, or already superseded), or
    None if this invocation closed the job at `max_attempts`. Original Redis
    fields are authoritative; payload/metadata arguments remain for caller
    compatibility but cannot replace the original admitted job on retry.

    Without a cap a "poison" job (one that fails on every attempt — e.g. a
    request the backend can't serve, or a transient that recurs) loops forever:
    fail → requeue → redeliver → fail, striking and evicting every worker that
    touches it (the 2026-06-16 gpt-oss "0 tokens" eviction cascade). Capping it
    turns an infinite loop into a clean per-client failure. Callers that have
    stronger evidence of a poison job use a tighter limit than MAX_REQUEUE."""
    status, delivery = await _handoff(
        {"job_id": job_id, "stream_id": stream_id, "stream": stream, "job_type": job_type},
        "failure",
        max_attempts,
    )
    return None if status == "closed" else delivery


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
                stream,
                CONSUMER_GROUP,
                "reclaimer",
                min_idle_time=_stale_ms_for(stream),
                start_id="0-0",
                count=10,
            )
            # result = (next_start_id, [(msg_id, fields), ...], [deleted_ids])
            if not result or not result[1]:
                continue

            for msg_id, fields in result[1]:
                status, _ = await _handoff(
                    {"job_id": fields["job_id"], "stream_id": msg_id, "stream": stream, "worker_id": "reclaimer"},
                    "stale",
                )
                reclaimed += status == "requeued"
        except Exception as e:
            logger.error("Error claiming stale jobs (%s)", error_type(e))
    return reclaimed
