# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebSocket endpoint for text workers.

Workers connect via WSS, receive jobs pushed from Redis Streams,
and stream tokens back. Each token is relayed to Redis Pub/Sub
so SSE clients (OpenAI/Anthropic endpoints) receive them in real time.

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

from ..auth import hash_api_key
from ..database import den_events_table, new_session, processing_gens_table, users_table, waiting_prompts_table, worker_models_table, workers_table
from ..redis_client import get_redis
from ..services import job_queue, token_stream
from ..services.den import calculate_den
from ..services.metrics_state import record_job_complete, record_job_failed

logger = logging.getLogger("grid_api.worker_ws")

router = APIRouter()

# Redis key prefixes for worker registry
WORKER_STATUS_PREFIX = "grid:worker:"
WORKER_STATUS_SUFFIX = ":status"
WORKER_ACTIVE_SET = "grid:workers:active"

# In-process tracking for WebSocket handles (can't serialize these to Redis)
_local_ws: dict[str, WebSocket] = {}


def _wallet_from_name(worker_name: str) -> str:
    """Best-effort extract an EVM wallet from the worker name.

    Convention used across AIPG workers: "MyWorker.0xABC..." or
    "MyWorker#0xABC...". Returns "" if no 0x-prefixed 40-hex address is found.
    Settlement can still resolve via worker->user mapping when this is empty.
    """
    import re

    if not worker_name:
        return ""
    m = re.search(r"0x[a-fA-F0-9]{40}", worker_name)
    return m.group(0) if m else ""


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
    """Refresh worker TTL in Redis."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.setex(key, 60, json.dumps(info))


async def get_available_models() -> list[str]:
    """Get all models from all connected workers (reads from Redis)."""
    r = get_redis()
    worker_ids = await r.smembers(WORKER_ACTIVE_SET)
    models = set()
    for wid in worker_ids:
        key = f"{WORKER_STATUS_PREFIX}{wid}{WORKER_STATUS_SUFFIX}"
        data = await r.get(key)
        if data:
            info = json.loads(data)
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
        # Optional explicit wallet; otherwise fall back to the wallet-in-name
        # convention "WorkerName.0xADDRESS" or "WorkerName#0xADDRESS".
        wallet_address = init_msg.get("wallet_address") or _wallet_from_name(worker_name)

        if not apikey or not worker_name:
            await ws.send_json({"type": "error", "message": "Missing apikey or name"})
            await ws.close(code=4001)
            return

        # Validate API key
        hashed_key = hash_api_key(apikey)
        async with await new_session() as session:
            result = await session.execute(
                sa.select(users_table).where(users_table.c.api_key == hashed_key)
            )
            user = result.mappings().first()

            if not user:
                await ws.send_json({"type": "error", "message": "Invalid API key"})
                await ws.close(code=4001)
                return

            # Find or create worker in DB
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
                        worker_type="text",
                        last_check_in=datetime.utcnow(),
                        max_length=max_length,
                        max_context_length=max_context_length,
                        threads=1,
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
            "max_length": max_length,
            "max_context_length": max_context_length,
            "wallet_address": wallet_address,
        }
        await register_worker(worker_id, worker_info)
        _local_ws[worker_id] = ws

        await ws.send_json({"type": "ready", "worker_id": worker_id})
        logger.info(f"Worker '{worker_name}' ({worker_id}) connected with models: {models}")

        # ── Step 2: Concurrent job polling + keepalive ──
        job_ready = asyncio.Event()
        pending_job = {}

        async def _poll_jobs():
            """Background: block on Redis for jobs, signal when one arrives."""
            while True:
                job = await job_queue.pop_job(worker_id, timeout_ms=5000)
                if job:
                    pending_job["data"] = job
                    job_ready.set()
                    while job_ready.is_set():
                        await asyncio.sleep(0.1)

        poll_task = asyncio.create_task(_poll_jobs())

        try:
            while True:
                try:
                    await asyncio.wait_for(job_ready.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

                # Keepalive ping
                await ws.send_json({"type": "ping"})
                try:
                    await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

                # Refresh Redis registry
                await refresh_worker(worker_id, worker_info)

                if not job_ready.is_set():
                    continue

                job = pending_job.pop("data", None)
                job_ready.clear()
                if not job:
                    continue

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

                # Create or update processing_gen (may already exist from a requeued job)
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
                full_text, token_count = await _handle_worker_generation(ws, job, worker_info)
                gen_time = _time.time() - gen_start

                # Job completed successfully
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
                server_token_count = len(full_text.split())
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
                async with await new_session() as session:
                    await session.execute(
                        sa.update(processing_gens_table)
                        .where(processing_gens_table.c.id == job["job_id"])
                        .values(generation=full_text, faulted=False)
                    )
                    # Persist den to the durable ledger the settlement bot
                    # pays against. Without this row, den is computed and
                    # discarded and the worker can never be paid.
                    await session.execute(
                        sa.insert(den_events_table).values(
                            job_id=job["job_id"],
                            worker_id=worker_id,
                            wallet_address=worker_info.get("wallet_address", ""),
                            model=selected_model,
                            den=den_awarded,
                            output_tokens=effective_tokens,
                            created=datetime.utcnow(),
                        )
                    )
                    await session.commit()

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
            )

        if worker_info:
            logger.info(f"Worker '{worker_info['name']}' cleaned up")


async def _handle_worker_generation(ws: WebSocket, job: dict, worker_info: dict) -> tuple[str, int]:
    """Receive tokens from worker and relay to Redis Pub/Sub + buffer.

    Returns (full_text, token_count).
    """
    job_id = job["job_id"]
    full_text = ""
    token_count = 0

    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=300)
        msg_type = msg.get("type")

        if msg_type == "token":
            text = msg.get("text", "")
            full_text += text
            token_count += 1
            await token_stream.publish_token(job_id, text)

        elif msg_type == "done":
            full_text = msg.get("full_text", full_text)
            await token_stream.publish_done(job_id, full_text)
            return full_text, token_count

        elif msg_type == "pong":
            continue

        elif msg_type == "error":
            logger.error(f"Worker error on job {job_id}: {msg.get('message')}")
            await token_stream.publish_error(job_id, msg.get("message", "Worker error"))
            return full_text, token_count
