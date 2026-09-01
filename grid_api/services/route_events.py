# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Best-effort, privacy-safe route-event outbox.

Production routing never awaits this module. Once accepted by Redis, events are
retained for isolated consumer-group processing subject to the emergency stream
bound. Capture failures degrade to bounded logs; they never alter dispatch or
settlement.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..config import get_settings
from ..redis_client import WORKER_ACTIVE_SET_KEY, get_redis
from ..safe_logging import error_type

logger = logging.getLogger("grid_api.route_events")

STREAM_KEY = "grid:validator:shadow-route-events"
MAX_STREAM_LEN = 10_000
MAX_CANDIDATES = 256
MAX_CANDIDATE_BYTES = 128_000
MAX_ACTIVE_WORKERS = 4096
MAX_PENDING_TASKS = 2048
CAPTURE_TIMEOUT_SECONDS = 5.0
REGISTRY_CACHE_SECONDS = 2.0
REGISTRY_FAILURE_BACKOFF_SECONDS = 1.0
CANDIDATE_BASIS = "post_dispatch_connected_compatible_replicas.v1"
_WORKER_STATUS_PREFIX = "grid:worker:"
_WORKER_STATUS_SUFFIX = ":status"
_pending: set[asyncio.Task] = set()
_registry_cache: tuple[float, tuple[dict[str, Any], ...]] | None = None
_registry_failure_until = 0.0
_registry_cache_lock: asyncio.Lock | None = None
_registry_cache_loop: asyncio.AbstractEventLoop | None = None


def _enabled() -> bool:
    return bool(get_settings().validator_shadow_observer_enabled)


def _secret() -> str:
    value = get_settings().validator_shadow_route_hmac_secret
    return value.get_secret_value() if value is not None else ""


def _route_ref(job: dict[str, Any]) -> str:
    stream = str(job.get("stream") or "")
    stream_id = str(job.get("stream_id") or "")
    job_id = str(job.get("job_id") or "")
    secret = _secret()
    if not secret or not job_id or not stream_id:
        raise ValueError("route commitment inputs are unavailable")
    material = f"{job_id}:{stream}:{stream_id}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _monotonic() -> float:
    return asyncio.get_running_loop().time()


def _cache_lock() -> asyncio.Lock:
    """Return a lock bound to the current application or test event loop."""
    global _registry_cache, _registry_cache_lock, _registry_cache_loop, _registry_failure_until
    loop = asyncio.get_running_loop()
    if _registry_cache_lock is None or _registry_cache_loop is not loop:
        _registry_cache_lock = asyncio.Lock()
        _registry_cache_loop = loop
        _registry_cache = None
        _registry_failure_until = 0.0
    return _registry_cache_lock


def _task_class(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = str(job.get("job_type") or "text")
    if job_type != "text":
        return job_type[:64]
    prompt = str(payload.get("prompt") or "")
    if not prompt:
        return "passthrough"
    # Keep classification local and transient; prompts never enter the outbox.
    from .router import _approx_tokens, classify

    return str(classify(prompt, _approx_tokens(prompt)))[:64]


def _capability(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = str(job.get("job_type") or "text")
    if job_type == "image":
        return "image.fidelity.v1"
    if job_type == "video":
        return "video.fidelity.v1"
    if job_type == "audio":
        return "audio.generation.v1"
    if job_type == "3d":
        return "3d.generation.v1"

    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    if request.get("tools"):
        return "text.tool_call.v1"
    response_format = request.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}:
        return "text.structured.v1"
    if request.get("stop"):
        return "text.stop_sequence.v1"
    return "text.instruction.v1"


async def _worker_registry_snapshot() -> tuple[dict[str, Any], ...]:
    """Read a minimal worker registry snapshot once per short burst window."""
    global _registry_cache, _registry_failure_until
    lock = _cache_lock()
    now = _monotonic()
    if _registry_cache is not None and now - _registry_cache[0] < REGISTRY_CACHE_SECONDS:
        return _registry_cache[1]
    if now < _registry_failure_until:
        raise RuntimeError("worker registry snapshot is in failure backoff")

    async with lock:
        now = _monotonic()
        if _registry_cache is not None and now - _registry_cache[0] < REGISTRY_CACHE_SECONDS:
            return _registry_cache[1]
        if now < _registry_failure_until:
            raise RuntimeError("worker registry snapshot is in failure backoff")
        try:
            redis = get_redis()
            ids = sorted(str(value) for value in await redis.smembers(WORKER_ACTIVE_SET_KEY))
            if len(ids) > MAX_ACTIVE_WORKERS:
                raise ValueError("active worker snapshot exceeds the observer bound")
            keys = [f"{_WORKER_STATUS_PREFIX}{worker_id}{_WORKER_STATUS_SUFFIX}" for worker_id in ids]
            raw_rows = await redis.mget(keys) if keys else []
            rows: list[dict[str, Any]] = []
            for worker_id, raw in zip(ids, raw_rows, strict=True):
                if not raw:
                    continue
                try:
                    info = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(info, dict):
                    continue
                candidate_id = str(info.get("worker_id") or worker_id)
                if not candidate_id or len(candidate_id) > 64:
                    continue
                rows.append(
                    {
                        "worker_id": candidate_id,
                        "models": tuple(str(value)[:255] for value in (info.get("models") or [])),
                        "job_types": tuple(str(value)[:16] for value in (info.get("job_types") or ["text"])),
                        "api_formats": tuple(str(value)[:64] for value in (info.get("api_formats") or ["openai-chat"])),
                    },
                )
        except Exception:
            _registry_failure_until = _monotonic() + REGISTRY_FAILURE_BACKOFF_SECONDS
            raise
        snapshot = tuple(rows)
        _registry_cache = (_monotonic(), snapshot)
        _registry_failure_until = 0.0
        return snapshot


async def _candidate_snapshot(
    *,
    job_type: str,
    api_format: str,
    selected_model: str,
    actual_worker_id: str,
) -> list[dict[str, Any]]:
    """Sample connected compatible replicas after dispatch.

    This is deliberately not described as the production scheduler's candidate
    set. Grid workers pull from shared streams, and this background sample may
    include busy replicas that could not have claimed the dispatched job.
    """
    actual = (str(actual_worker_id)[:64], str(selected_model)[:255])
    candidates: set[tuple[str, str]] = {actual}
    for info in await _worker_registry_snapshot():
        if actual[1] not in (info.get("models") or []):
            continue
        if job_type not in (info.get("job_types") or ["text"]):
            continue
        if job_type == "text" and api_format not in (info.get("api_formats") or ["openai-chat"]):
            continue
        candidates.add((str(info["worker_id"]), actual[1]))

    ordered = [actual]
    ordered.extend(sorted(candidates - {ordered[0]})[: MAX_CANDIDATES - 1])
    return [{"worker_id": worker_id, "model": model, "baseline_rank": rank} for rank, (worker_id, model) in enumerate(ordered)]


def _encoded_candidates(candidates: list[dict[str, Any]]) -> str:
    """Keep the actual route and a deterministic prefix within the wire bound."""
    bounded = candidates[:MAX_CANDIDATES]
    while bounded:
        encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= MAX_CANDIDATE_BYTES:
            return encoded
        bounded = bounded[:-1]
    raise ValueError("candidate snapshot cannot fit the observer payload bound")


def _route_capture(job: dict[str, Any]) -> dict[str, str]:
    """Reduce a live job to bounded observer fields before scheduling work."""
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return {
        "route_ref": _route_ref(job),
        "job_type": str(job.get("job_type") or "text")[:16],
        "api_format": str(payload.get("api_format") or "openai-chat")[:64],
        "task_class": _task_class(job),
        "capability": _capability(job),
    }


async def _emit_route(
    capture: dict[str, str],
    selected_model: str,
    worker_id: str,
    observed_at: str | None = None,
) -> None:
    try:
        async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
            candidates = await _candidate_snapshot(
                job_type=capture["job_type"],
                api_format=capture["api_format"],
                selected_model=selected_model,
                actual_worker_id=worker_id,
            )
            await get_redis().xadd(
                STREAM_KEY,
                {
                    "kind": "route",
                    "route_ref": capture["route_ref"],
                    "observed_at": observed_at or _iso_now(),
                    "task_class": capture["task_class"],
                    "modality": capture["job_type"],
                    "capability": capture["capability"],
                    "candidate_basis": CANDIDATE_BASIS,
                    "candidates": _encoded_candidates(candidates),
                    "actual_model": str(selected_model)[:255],
                    "actual_worker_id": str(worker_id)[:64],
                },
                maxlen=MAX_STREAM_LEN,
                approximate=True,
            )
    except Exception as exc:
        logger.warning("Route-event capture failed error_type=%s", error_type(exc))


async def _emit_outcome(
    route_ref: str,
    worker_id: str,
    terminal_status: str,
    duration_seconds: float | None,
    finished_at: str | None = None,
) -> None:
    try:
        async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
            duration_ms = ""
            if duration_seconds is not None:
                duration_ms = str(max(0, int(float(duration_seconds) * 1000)))
            await get_redis().xadd(
                STREAM_KEY,
                {
                    "kind": "outcome",
                    "route_ref": route_ref,
                    "finished_at": finished_at or _iso_now(),
                    "actual_worker_id": str(worker_id)[:64],
                    "terminal_status": str(terminal_status),
                    "duration_ms": duration_ms,
                },
                maxlen=MAX_STREAM_LEN,
                approximate=True,
            )
    except Exception as exc:
        logger.warning("Route-outcome capture failed error_type=%s", error_type(exc))


def _task_done(task: asyncio.Task) -> None:
    _pending.discard(task)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _schedule(coro) -> None:
    try:
        if not _enabled():
            coro.close()
            return
        if len(_pending) >= MAX_PENDING_TASKS:
            coro.close()
            logger.warning("Route-event capture queue is full")
            return
        task = asyncio.get_running_loop().create_task(coro)
    except Exception as exc:
        coro.close()
        logger.warning("Route-event scheduling failed error_type=%s", error_type(exc))
        return
    _pending.add(task)
    task.add_done_callback(_task_done)


def capture_route(*, job: dict[str, Any], selected_model: str, worker_id: str) -> None:
    """Schedule a route snapshot without awaiting or mutating the live route."""
    try:
        if not _enabled():
            return
        capture = _route_capture(job)
    except Exception as exc:
        logger.warning("Route-event preparation failed error_type=%s", error_type(exc))
        return
    _schedule(_emit_route(capture, str(selected_model), str(worker_id), _iso_now()))


def capture_outcome(
    *,
    job: dict[str, Any],
    worker_id: str,
    terminal_status: str,
    duration_seconds: float | None = None,
) -> None:
    """Schedule one route-attempt outcome without touching terminal authority."""
    try:
        if not _enabled():
            return
        route_ref = _route_ref(job)
    except Exception as exc:
        logger.warning("Route-outcome preparation failed error_type=%s", error_type(exc))
        return
    _schedule(
        _emit_outcome(
            route_ref,
            str(worker_id),
            str(terminal_status),
            duration_seconds,
            _iso_now(),
        ),
    )


async def drain(*, timeout_seconds: float = 2.0) -> None:
    """Bounded shutdown drain; request handling never waits on this."""
    if not _pending:
        return
    _, pending = await asyncio.wait(set(_pending), timeout=max(0.0, timeout_seconds))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
