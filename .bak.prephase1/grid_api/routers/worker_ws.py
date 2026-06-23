# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebSocket endpoint for grid workers — the unified worker protocol.

One protocol for every worker type. Workers register with the job types they
serve (text | image | video), receive jobs pushed from the per-type Redis
Streams, and report results back:

  text  — stream tokens; relayed to Redis Pub/Sub for SSE clients
  media — upload outputs directly to R2 via presigned PUT URLs included in
          the job message (workers never hold storage credentials), then
          report the object keys + content hashes

Every completion appends an event to the grid_ledger (den + prompt/result
hashes) — the source of truth the on-chain settlement pays against.

Worker registry is stored in Redis (not in-memory) so multiple
uvicorn processes can share state.
"""

import asyncio
import json
import logging
from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..database import (
    LEGACY_WORKER_DEFAULTS,
    new_session,
    processing_gens_table,
    users_table,
    waiting_prompts_table,
    worker_models_table,
    workers_table,
)
from ..redis_client import get_redis
from ..services import accounts as accounts_svc
from ..services import job_queue, storage, token_stream
from ..services import ledger as ledger_svc
from ..services.den import calculate_den, calculate_media_den, count_tokens
from ..v2.schema import workers as v2_workers_table
from ..services.metrics_state import record_job_complete, record_job_failed

logger = logging.getLogger("grid_api.worker_ws")

router = APIRouter()

# Redis key prefixes for worker registry
WORKER_STATUS_PREFIX = "grid:worker:"
WORKER_STATUS_SUFFIX = ":status"
WORKER_ACTIVE_SET = "grid:workers:active"

# In-process tracking for WebSocket handles (can't serialize these to Redis)
_local_ws: dict[str, WebSocket] = {}

# ── Worker health enforcement ──
# A completion that is empty (zero tokens) or an explicit worker error counts
# as a strike. A worker that accumulates MAX_STRIKES within the decay window is
# evicted and barred from re-registering for EVICT_COOLDOWN_S — this is what
# stops a worker whose inference backend has died from silently swallowing a
# share of every job (the 2026-06-14 "empty every other message" outage).
MAX_STRIKES = 3
STRIKE_DECAY_S = 300       # strikes reset after 5 min without a failure
EVICT_COOLDOWN_S = 120     # barred from re-registering after eviction


async def _record_strike(worker_id: str) -> int:
    """Increment a worker's failure strike count; returns the new total."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}:strikes"
    n = await r.incr(key)
    await r.expire(key, STRIKE_DECAY_S)
    return n


async def _clear_strikes(worker_id: str):
    """A successful job clears the strike count."""
    r = get_redis()
    await r.delete(f"{WORKER_STATUS_PREFIX}{worker_id}:strikes")


async def _evict_worker(worker_id: str, worker_name: str):
    """Deregister an unhealthy worker and bar it from re-registering briefly."""
    r = get_redis()
    await unregister_worker(worker_id)
    await r.setex(f"grid:worker:cooldown:{worker_name}", EVICT_COOLDOWN_S, "evicted")
    await r.delete(f"{WORKER_STATUS_PREFIX}{worker_id}:strikes")


async def _is_in_cooldown(worker_name: str) -> bool:
    r = get_redis()
    return bool(await r.get(f"grid:worker:cooldown:{worker_name}"))


# ── Redis-backed worker registry ──


async def register_worker(worker_id: str, info: dict):
    """Register a worker in Redis. Visible to all uvicorn processes."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.setex(key, 60, json.dumps(info))
    await r.sadd(WORKER_ACTIVE_SET, worker_id)


async def unregister_worker(worker_id: str):
    """Remove a worker from the Redis registry."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.delete(key)
    await r.srem(WORKER_ACTIVE_SET, worker_id)


async def refresh_worker(worker_id: str, info: dict):
    """Refresh worker TTL in Redis AND re-assert active-set membership.

    Re-adding to the set on every refresh self-heals a reconnect race: when a
    worker reconnects under the same worker_id, the OLD handler's cleanup
    (unregister_worker: srem + del) can run AFTER the NEW handler's
    register_worker, leaving a live status key that's missing from
    grid:workers:active — making the worker invisible to get_available_models
    (so /v1/models returns [] and chat 503s) even though it's connected and
    healthy. Re-asserting membership here repairs that within one refresh (~10s).
    """
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.setex(key, 60, json.dumps(info))
    await r.sadd(WORKER_ACTIVE_SET, worker_id)


async def get_available_models(job_type: str | None = None) -> list[str]:
    """Get models from connected workers (reads from Redis).

    When `job_type` is given (e.g. "text"), only models served by a worker of
    that modality are returned — so the OpenAI `/v1/models` chat list never
    surfaces image/video models like LTX-2.3 that can't be used via
    chat-completions. Each worker self-declares its `job_types` at registration.
    """
    r = get_redis()
    worker_ids = await r.smembers(WORKER_ACTIVE_SET)
    models = set()
    for wid in worker_ids:
        key = f"{WORKER_STATUS_PREFIX}{wid}{WORKER_STATUS_SUFFIX}"
        data = await r.get(key)
        if data:
            info = json.loads(data)
            if job_type and job_type not in (info.get("job_types") or ["text"]):
                continue
            models.update(info.get("models", []))
        else:
            # Stale entry — worker expired
            await r.srem(WORKER_ACTIVE_SET, wid)
    return sorted(models)


async def get_connected_worker_count() -> int:
    """Get count of active workers."""
    r = get_redis()
    return await r.scard(WORKER_ACTIVE_SET)


# ── WebSocket handler ──


@router.websocket("/v1/workers/ws")
async def worker_websocket(ws: WebSocket):
    """Persistent WebSocket connection for text generation workers."""
    await ws.accept()
    worker_info = None
    worker_id = None
    current_job = None  # Track in-progress job for retry on disconnect

    try:
        # ── Step 1: Auth handshake ──
        init_msg = await asyncio.wait_for(ws.receive_json(), timeout=30)

        apikey = init_msg.get("apikey", "")
        worker_name = init_msg.get("name", "")
        models = init_msg.get("models", [])
        max_length = init_msg.get("max_length", 512)
        max_context_length = init_msg.get("max_context_length", 2048)
        # Job types this worker serves. Accepts the new `job_types` list or the
        # legacy single `worker_type`; defaults to text for old text workers.
        job_types = init_msg.get("job_types") or [init_msg.get("worker_type", "text")]
        job_types = [t for t in job_types if t in ("text", "image", "video")] or ["text"]
        # NOTE: the payout wallet is NOT taken from the worker. It's resolved
        # from the authenticated account below. This means an operator runs
        # workers on any number of rigs with ONLY an API key — no wallet or
        # private key on the rig — and a worker can't declare a wallet to
        # redirect another account's earnings.

        if not apikey or not worker_name:
            await ws.send_json({"type": "error", "message": "Missing apikey or name"})
            await ws.close(code=4001)
            return

        # Validate API key — v2 account keys first, legacy keys fall back.
        user = await accounts_svc.resolve_api_key(apikey)
        if not user:
            await ws.send_json({"type": "error", "message": "Invalid API key"})
            await ws.close(code=4001)
            return

        # Refuse workers we just evicted for failing health — gives a flapping
        # worker (dead backend) time to actually recover before rejoining.
        if await _is_in_cooldown(worker_name):
            await ws.send_json({
                "type": "error",
                "message": "Worker recently evicted for failed generations; retry shortly.",
            })
            await ws.close(code=4003)
            return

        # Payout wallet ALWAYS comes from the authenticated account (set once by
        # the operator via SIWE on the dashboard), never from the worker. If the
        # account has no wallet yet, den is still recorded and accrues
        # unattributed until they set one (see settlement.count_unattributed_den).
        wallet_address = user.get("wallet") or ""

        if user["source"] == "v2":
            # v2 workers live in grid_workers (wallet-keyed, JSON models).
            now = datetime.utcnow()
            async with await new_session() as session:
                row = (
                    await session.execute(
                        sa.select(v2_workers_table.c.id).where(
                            v2_workers_table.c.name == worker_name
                        )
                    )
                ).first()
                if row:
                    worker_id = str(row[0])
                    await session.execute(
                        sa.update(v2_workers_table)
                        .where(v2_workers_table.c.id == row[0])
                        .values(
                            last_seen=now,
                            models=models,
                            wallet=wallet_address or None,
                            type=job_types[0],
                        )
                    )
                else:
                    worker_id = str(uuid4())
                    await session.execute(
                        sa.insert(v2_workers_table).values(
                            id=worker_id,
                            account_id=user["account_id"],
                            name=worker_name,
                            type=job_types[0],
                            wallet=wallet_address or None,
                            models=models,
                            capabilities={"job_types": job_types},
                            bridge_agent=init_msg.get("bridge_agent", "grid-ws"),
                            maintenance=False,
                            first_seen=now,
                            last_seen=now,
                            jobs_completed=0,
                            den_earned=0.0,
                        )
                    )
                await session.commit()
        else:
            # Legacy keys: horde workers table (Haidra bookkeeping).
            async with await new_session() as session:
                result = await session.execute(
                    sa.select(workers_table).where(
                        workers_table.c.name == worker_name,
                        workers_table.c.user_id == user["id"],
                    )
                )
                worker = result.mappings().first()

                if worker:
                    worker_id = str(worker["id"])
                    await session.execute(
                        sa.update(workers_table)
                        .where(workers_table.c.id == worker["id"])
                        .values(
                            last_check_in=datetime.utcnow(),
                            max_length=max_length,
                            max_context_length=max_context_length,
                        )
                    )
                else:
                    worker_id = str(uuid4())
                    await session.execute(
                        sa.insert(workers_table).values(
                            id=worker_id,
                            user_id=user["id"],
                            name=worker_name,
                            worker_type=job_types[0],
                            last_check_in=datetime.utcnow(),
                            max_length=max_length,
                            max_context_length=max_context_length,
                            threads=1,
                            nsfw=False,
                            maintenance=False,
                            paused=False,
                            bridge_agent=init_msg.get("bridge_agent", "grid-ws"),
                            **LEGACY_WORKER_DEFAULTS,
                        )
                    )

                # Update model list
                await session.execute(
                    sa.delete(worker_models_table).where(worker_models_table.c.worker_id == worker_id)
                )
                for model in models:
                    await session.execute(
                        sa.insert(worker_models_table).values(worker_id=worker_id, model=model)
                    )
                await session.commit()

        # Register in Redis (visible to all processes)
        worker_info = {
            "worker_id": worker_id,
            "user_id": user["id"],
            "name": worker_name,
            "models": models,
            "job_types": job_types,
            "max_length": max_length,
            "max_context_length": max_context_length,
            "wallet_address": wallet_address,
        }
        await register_worker(worker_id, worker_info)
        _local_ws[worker_id] = ws

        await ws.send_json({"type": "ready", "worker_id": worker_id})
        logger.info(
            f"Worker '{worker_name}' ({worker_id}) connected, types={job_types}, models: {models}"
        )

        # ── Step 2: Concurrent job polling + keepalive ──
        # A bounded queue (maxsize=1) decouples the Redis poll from the
        # dispatch loop with natural backpressure: at most one job is
        # prefetched. This replaces an earlier job_ready/busy-wait handshake
        # that could deadlock the poll task after the first dispatch (it spun
        # forever in `while job_ready.is_set()`), so it stopped calling
        # XREADGROUP entirely — silently stranding every later job while the
        # worker still looked online.
        local_jobs: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def _poll_jobs():
            """Background: pull jobs from Redis and hand them to the loop.

            Wrapped so a transient Redis/parse error can NEVER silently kill
            this task — if it died, the main loop kept refreshing registration
            (worker looks online) while no jobs were ever consumed again. That
            was the 'serves one job then goes deaf' bug.
            """
            while True:
                try:
                    job = await job_queue.pop_job(worker_id, timeout_ms=5000, job_types=job_types)
                    if job:
                        await local_jobs.put(job)  # blocks (backpressure) until taken
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"_poll_jobs error for {worker_id}: {e}", exc_info=True)
                    await asyncio.sleep(1)

        poll_task = asyncio.create_task(_poll_jobs())

        try:
            while True:
                # Wait for the next job, or time out every 10s to keepalive.
                try:
                    job = await asyncio.wait_for(local_jobs.get(), timeout=10)
                except asyncio.TimeoutError:
                    job = None

                # Refresh registration every iteration regardless of socket
                # health (cheap Redis write; keeps the worker in the registry
                # even if the WS is momentarily slow).
                await refresh_worker(worker_id, worker_info)

                if job is None:
                    # Idle keepalive — BOUNDED. On a half-open connection a raw
                    # ws.send_json can block until the kernel TCP timeout
                    # (minutes), wedging the loop; time-box it and break cleanly
                    # on any failure so the worker reconnects + re-registers.
                    try:
                        await asyncio.wait_for(ws.send_json({"type": "ping"}), timeout=10)
                        try:
                            await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass
                    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError) as e:
                        logger.info(
                            f"Worker '{worker_name}' keepalive failed "
                            f"({type(e).__name__}) — closing for reconnect"
                        )
                        break
                    continue

                # Got a job → dispatch it (model check + text/media paths below).

                # Check model compatibility. The shared stream + consumer group
                # hands jobs to a random worker regardless of served models, so
                # a mismatch is normal in a heterogeneous pool. Requeue for
                # another worker instead of discarding (which would strand the
                # client). If the job has bounced past the requeue limit, no
                # worker serves the model — fault it and tell the client.
                job_models = job["models"]
                matching = [m for m in job_models if m in models] if job_models else models
                if not matching:
                    requeued = await job_queue.requeue_for_mismatch(job)
                    if not requeued:
                        await token_stream.publish_error(
                            job["job_id"],
                            f"No worker available for the requested model(s): "
                            f"{', '.join(job_models) if job_models else 'unspecified'}.",
                        )
                    continue

                selected_model = matching[0]

                # Track current job for retry on disconnect
                current_job = job

                # ── Media path (image/video) ──
                if job.get("job_type", "text") != "text":
                    ok = await _handle_media_job(ws, job, selected_model, worker_id, worker_info)
                    if ok:
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None
                    continue

                # ── Text path ──
                # Legacy bookkeeping rows (processing_gens FKs onto
                # waiting_prompts) only exist for jobs submitted with legacy
                # keys; v2-key jobs carry _legacy_rows=False and skip them.
                legacy_rows = bool(job["payload"].get("_legacy_rows", True))

                # Create or update processing_gen (may already exist from a requeued job)
                if legacy_rows:
                  async with await new_session() as session:
                    existing = await session.execute(
                        sa.select(processing_gens_table.c.id).where(
                            processing_gens_table.c.id == job["job_id"]
                        )
                    )
                    if existing.first():
                        await session.execute(
                            sa.update(processing_gens_table)
                            .where(processing_gens_table.c.id == job["job_id"])
                            .values(
                                worker_id=worker_id,
                                model=selected_model,
                                start_time=datetime.utcnow(),
                                faulted=False,
                                cancelled=False,
                            )
                        )
                    else:
                        await session.execute(
                            sa.insert(processing_gens_table).values(
                                id=job["job_id"],
                                procgen_type="text",
                                wp_id=job["job_id"],
                                worker_id=worker_id,
                                model=selected_model,
                                seed=0,
                                start_time=datetime.utcnow(),
                                created=datetime.utcnow(),
                                cancelled=False,
                                faulted=False,
                                fake=False,
                                censored=False,
                                job_ttl=150,
                                progress_percent=0,
                                current_step=0,
                                total_steps=0,
                                media_type="text",
                            )
                        )
                    await session.commit()

                await ws.send_json({
                    "type": "job",
                    "id": job["job_id"],
                    "model": selected_model,
                    "payload": job["payload"],
                })

                # Wait for tokens + done
                import time as _time
                gen_start = _time.time()
                full_text, token_count, failed = await _handle_worker_generation(ws, job, worker_info)
                gen_time = _time.time() - gen_start

                if failed:
                    # The worker couldn't serve this job (dead backend / empty
                    # output). Strike it, and recover the job: if nothing was
                    # streamed to the client yet, silently requeue so a healthy
                    # worker can serve it; otherwise surface the error. NEVER pay
                    # den for a failed generation.
                    strikes = await _record_strike(worker_id)
                    record_job_failed()
                    if token_count == 0:
                        await job_queue.requeue_job(
                            job["job_id"], job["payload"], job["models"],
                            job.get("stream_id"), job_type="text", stream=job.get("stream"),
                        )
                        logger.warning(
                            f"Job {job['job_id']} requeued after worker '{worker_name}' "
                            f"failed it (strike {strikes}/{MAX_STRIKES})"
                        )
                    else:
                        await token_stream.publish_error(
                            job["job_id"], "Worker failed mid-generation; please retry."
                        )
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None

                    if strikes >= MAX_STRIKES:
                        logger.error(
                            f"Worker '{worker_name}' ({worker_id}) hit {MAX_STRIKES} "
                            f"strikes — evicting and barring re-register for {EVICT_COOLDOWN_S}s"
                        )
                        await _evict_worker(worker_id, worker_name)
                        break  # drop the WS; cooldown blocks immediate rejoin
                    continue

                # Job completed successfully — clear any prior strikes.
                await _clear_strikes(worker_id)
                current_job = None
                await job_queue.ack_job(job["stream_id"])

                # Calculate den reward — but harden against gaming first.
                #
                # 1) Output tokens: NEVER trust the worker's self-reported count
                #    (a malicious worker inflates it). Count server-side from
                #    the text we actually received, and cap at the job's
                #    requested max_length — a worker can't be credited for more
                #    output than was asked for.
                requested_max = int(job["payload"].get("max_length", 512) or 512)
                # Real tokenizer (tiktoken) server-side — worker-independent and
                # far more accurate than word-splitting (which undercounts ~25%).
                server_token_count = count_tokens(full_text)
                effective_tokens = min(server_token_count, token_count or server_token_count, requested_max)

                # 2) Context: the prompt is user-controlled and the context
                #    multiplier scales up to 30x. Cap the prompt token count at
                #    the worker's advertised max_context_length so a self-dealer
                #    can't farm den by sending an enormous prompt to their own
                #    worker.
                prompt_text = job["payload"].get("prompt", "")
                ctx_cap = int(worker_info.get("max_context_length", 2048) or 2048)
                prompt_tokens = min(len(prompt_text.split()), ctx_cap)

                # NOTE: the model-size multiplier still derives from the model
                # NAME (den.py), which a worker advertising a fake large-model
                # name could inflate via self-dealing. The real fix is to source
                # the param count from the on-chain ModelVault registry rather
                # than the name. Tracked separately — bounded here only by the
                # token/context caps above.
                den_awarded = calculate_den(
                    output_tokens=effective_tokens,
                    prompt_tokens=prompt_tokens,
                    model_name=selected_model,
                    generation_time_seconds=gen_time,
                )
                if legacy_rows:
                  async with await new_session() as session:
                    await session.execute(
                        sa.update(processing_gens_table)
                        .where(processing_gens_table.c.id == job["job_id"])
                        .values(generation=full_text, faulted=False)
                    )
                    await session.commit()

                # Append the completion to the grid_ledger — the source of
                # truth the on-chain settlement pays against, carrying the
                # prompt/result hashes that make the work attestable.
                await ledger_svc.record_completion(
                    job_id=job["job_id"],
                    worker_id=worker_id,
                    wallet=worker_info.get("wallet_address", ""),
                    model=selected_model,
                    job_type="text",
                    den=den_awarded,
                    output_units=effective_tokens,
                    prompt_hash=ledger_svc.text_hash(prompt_text),
                    result_hash=ledger_svc.text_hash(full_text),
                )

                await ws.send_json({
                    "type": "ack",
                    "id": job["job_id"],
                    "den": den_awarded,
                })

                # Record metrics
                record_job_complete(tokens=effective_tokens, den=den_awarded, duration=gen_time)
        finally:
            poll_task.cancel()

    except WebSocketDisconnect as e:
        logger.info(f"Worker '{worker_info['name'] if worker_info else 'unknown'}' disconnected (code={e.code})")
    except asyncio.TimeoutError:
        logger.warning(f"Worker '{worker_info['name'] if worker_info else 'unknown'}' timed out during handshake")
    except Exception as e:
        logger.error(f"Worker WebSocket error [{type(e).__name__}]: {e}", exc_info=True)
    finally:
        # ── Cleanup + job retry ──
        if worker_id:
            _local_ws.pop(worker_id, None)
            await unregister_worker(worker_id)

        if current_job:
            # Worker disconnected with a job in progress — notify client and requeue
            job_id = current_job["job_id"]
            logger.warning(f"Worker disconnected with job {job_id} in progress — sending error to client and requeuing")
            record_job_failed()
            await token_stream.publish_error(job_id, "Worker disconnected during generation. Job requeued.")
            await job_queue.requeue_job(
                job_id,
                current_job["payload"],
                current_job["models"],
                current_job.get("stream_id"),
                job_type=current_job.get("job_type", "text"),
                stream=current_job.get("stream"),
            )

        if worker_info:
            logger.info(f"Worker '{worker_info['name']}' cleaned up")


async def _handle_media_job(
    ws: WebSocket, job: dict, selected_model: str, worker_id: str, worker_info: dict
) -> bool:
    """Dispatch one image/video job to the worker and collect the result.

    The job message carries presigned PUT slots so the worker uploads outputs
    straight to R2. Completion is published on the job's token-stream channel
    as a JSON `full_text` payload, which the waiting HTTP handler parses.

    Returns True if the job finished (success or clean failure published to
    the client) and should be acked; raising propagates a socket failure so
    the caller's disconnect path requeues the job.
    """
    import time as _time

    job_id = job["job_id"]
    payload = job["payload"]
    job_type = job.get("job_type", "image")

    n = int(payload.get("n", 1) or 1)
    ext = payload.get("ext") or ("mp4" if job_type == "video" else "webp")
    try:
        upload_slots = storage.presign_outputs(job_id, n, ext)
    except Exception as e:
        logger.error(f"Presign failed for job {job_id}: {e}")
        await token_stream.publish_error(job_id, "Storage unavailable; please retry.")
        return True

    await ws.send_json({
        "type": "job",
        "id": job_id,
        "job_type": job_type,
        "model": selected_model,
        "payload": payload,
        "upload": [
            {"put_url": s["put_url"], "key": s["key"], "content_type": s["content_type"]}
            for s in upload_slots
        ],
    })

    gen_start = _time.time()
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=600)
        msg_type = msg.get("type")

        if msg_type == "progress":
            # Relayed on the token channel so a future SSE progress endpoint
            # can stream it; the blocking HTTP handler simply ignores tokens.
            await token_stream.publish_token(
                job_id,
                json.dumps({"progress": msg.get("pct", 0), "preview": msg.get("preview_b64")}),
            )

        elif msg_type == "done":
            gen_time = _time.time() - gen_start
            reported = {r.get("index", i): r for i, r in enumerate(msg.get("results", []))}
            outputs = []
            for i, slot in enumerate(upload_slots):
                rep = reported.get(i, {})
                outputs.append({
                    "url": slot["public_url"],
                    "key": slot["key"],
                    "seed": rep.get("seed"),
                    "sha256": rep.get("sha256"),
                })

            den_awarded = calculate_media_den(
                job_type=job_type,
                width=int(payload.get("width", 1024) or 1024),
                height=int(payload.get("height", 1024) or 1024),
                steps=int(payload.get("steps", 20) or 20),
                n=n,
                frames=int(payload.get("frames", 0) or 0),
            )

            # Result hash: deterministic digest over the worker-reported
            # per-output sha256s (server never sees media bytes — verifiable
            # later by fetching the R2 objects).
            result_hash = ledger_svc.canonical_hash(
                [o.get("sha256") or o["key"] for o in outputs]
            )
            await ledger_svc.record_completion(
                job_id=job_id,
                worker_id=worker_id,
                wallet=worker_info.get("wallet_address", ""),
                model=selected_model,
                job_type=job_type,
                den=den_awarded,
                output_units=max(n, int(payload.get("frames", 0) or 0)),
                prompt_hash=ledger_svc.canonical_hash(payload),
                result_hash=result_hash,
            )

            await token_stream.publish_done(
                job_id, json.dumps({"media": outputs, "model": selected_model})
            )
            await ws.send_json({"type": "ack", "id": job_id, "den": den_awarded})
            record_job_complete(tokens=0, den=den_awarded, duration=gen_time)
            return True

        elif msg_type == "pong":
            continue

        elif msg_type == "error":
            logger.error(f"Worker error on media job {job_id}: {msg.get('message')}")
            await token_stream.publish_error(job_id, msg.get("message", "Worker error"))
            record_job_failed()
            return True


async def _handle_worker_generation(ws: WebSocket, job: dict, worker_info: dict) -> tuple[str, int, bool]:
    """Receive tokens from worker and relay to Redis Pub/Sub + buffer.

    Returns (full_text, token_count, failed). On SUCCESS this publishes the
    DONE sentinel to the client. On FAILURE (explicit worker error, or a
    completion that produced no output at all) it publishes NOTHING and returns
    failed=True — the caller decides whether to silently requeue to a healthy
    worker (when nothing was streamed yet) or surface the error to the client.
    """
    job_id = job["job_id"]
    full_text = ""
    full_reasoning = ""
    token_count = 0

    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=300)
        msg_type = msg.get("type")

        if msg_type == "token":
            text = msg.get("text", "")
            is_reasoning = bool(msg.get("reasoning", False))
            # Keep reasoning and answer in separate channels (faithful passthrough).
            if is_reasoning:
                full_reasoning += text
            else:
                full_text += text
            token_count += 1
            await token_stream.publish_token(job_id, text, reasoning=is_reasoning)

        elif msg_type == "done":
            full_text = msg.get("full_text", full_text)
            full_reasoning = msg.get("full_reasoning", full_reasoning)
            # An empty completion (no content AND no reasoning) is a silent backend
            # failure, not a success — don't pay for it or hand the client a blank reply.
            if token_count == 0 and not (full_text or "").strip() and not (full_reasoning or "").strip():
                logger.warning(f"Worker returned EMPTY completion for job {job_id} — treating as failure")
                return full_text, token_count, True
            await token_stream.publish_done(job_id, full_text, full_reasoning)
            return full_text, token_count, False

        elif msg_type == "pong":
            continue

        elif msg_type == "error":
            logger.error(f"Worker error on job {job_id}: {msg.get('message')}")
            return full_text, token_count, True
