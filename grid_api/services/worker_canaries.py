# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Economically inert, hard-targeted worker connectivity canaries.

These canaries prove that Core can route a synthetic request through one exact
registered text worker and receive the randomized response. They are setup
evidence only: they do not prove model identity, intelligence, or quality.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from uuid import uuid4

from ..redis_client import get_redis
from . import job_queue, token_stream

CANARY_COOLDOWN_SECONDS = 300
CANARY_TIMEOUT_SECONDS = 180
CANARY_MAX_OUTPUT_CHARS = 256
_CANARY_PREFIX = "grid:worker:self-canary:"


class WorkerCanaryError(RuntimeError):
    """Base class for bounded operator-facing canary failures."""


class WorkerCanaryRateLimited(WorkerCanaryError):
    """The exact worker recently started another self-canary."""


class WorkerCanaryUnavailable(WorkerCanaryError):
    """Core could not safely start or observe the canary."""


async def _acquire_canary_slot(worker_id: str) -> None:
    try:
        acquired = await get_redis().set(
            f"{_CANARY_PREFIX}{worker_id}",
            "1",
            ex=CANARY_COOLDOWN_SECONDS,
            nx=True,
        )
    except Exception as exc:
        raise WorkerCanaryUnavailable("canary gate unavailable") from exc
    if not acquired:
        raise WorkerCanaryRateLimited("worker canary recently started")


def _result(
    *,
    status: str,
    worker_name: str,
    model: str,
    latency_ms: int | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": "aipg.worker.canary.v1",
        "status": status,
        "worker_name": worker_name,
        "model": model,
        "latency_ms": latency_ms,
        "reason": reason,
        "proof_scope": "hard_targeted_connectivity_and_exact_output",
        "quality_claim": "none",
        "economic_effect": "none",
    }


async def run_text_connectivity_canary(
    *,
    worker_id: str,
    worker_name: str,
    model: str,
) -> dict[str, Any]:
    """Run one randomized, no-fallback text round trip on an exact worker."""
    await _acquire_canary_slot(worker_id)

    canary_id = f"self_{uuid4().hex}"
    grid_nonce = secrets.token_urlsafe(24)
    expected = f"aipg-{secrets.token_hex(10)}"
    prompt = f"Reply with exactly this token and nothing else: {expected}"
    job_id = str(uuid4())
    payload = {
        "request": {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32,
            "temperature": 0,
            "stream": True,
        },
        "api_format": "openai-chat",
        "prompt": prompt,
        "max_length": 32,
        "temperature": 0,
        "_worker_self_canary": True,
        "_worker_self_canary_id": canary_id,
        "_worker_self_canary_nonce": grid_nonce,
    }
    started = time.monotonic()
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            [model],
            job_type="text",
            preferred_worker=worker_name,
            hard_target_worker=worker_name,
        )
    except Exception as exc:
        raise WorkerCanaryUnavailable("canary dispatch unavailable") from exc

    chunks: list[str] = []
    observed_chars = 0
    captured_chars = 0
    output_too_long = False
    try:
        async for event in token_stream.subscribe_tokens(
            job_id,
            timeout=CANARY_TIMEOUT_SECONDS,
        ):
            if event.get("error"):
                return _result(
                    status="failed",
                    worker_name=worker_name,
                    model=model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    reason="worker_or_transport_error",
                )
            if event.get("text") != token_stream.DONE_SENTINEL:
                chunk = token_stream.event_content_text(event)
                if not isinstance(chunk, str):
                    return _result(
                        status="failed",
                        worker_name=worker_name,
                        model=model,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        reason="malformed_output",
                    )
                observed_chars += len(chunk)
                remaining = CANARY_MAX_OUTPUT_CHARS - captured_chars
                if remaining > 0:
                    captured = chunk[:remaining]
                    chunks.append(captured)
                    captured_chars += len(captured)
                output_too_long = output_too_long or observed_chars > CANARY_MAX_OUTPUT_CHARS
                continue

            grid = event.get("grid") if isinstance(event.get("grid"), dict) else {}
            if (
                str(grid.get("worker_id") or "") != worker_id
                or str(grid.get("canary_id") or "") != canary_id
                or str(grid.get("grid_nonce") or "") != grid_nonce
                or grid.get("economic_effect") != "none"
            ):
                return _result(
                    status="failed",
                    worker_name=worker_name,
                    model=model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    reason="binding_mismatch",
                )
            full_text = event.get("full_text")
            if full_text is not None and not isinstance(full_text, str):
                return _result(
                    status="failed",
                    worker_name=worker_name,
                    model=model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    reason="malformed_output",
                )
            if isinstance(full_text, str) and len(full_text) > CANARY_MAX_OUTPUT_CHARS:
                output_too_long = True
            output = (full_text or "".join(chunks))[:CANARY_MAX_OUTPUT_CHARS].strip()
            if len(output) >= 2 and output[0] == output[-1] and output[0] in {'"', "'"}:
                output = output[1:-1].strip()
            exact = not output_too_long and secrets.compare_digest(output, expected)
            return _result(
                status="passed" if exact else "failed",
                worker_name=worker_name,
                model=model,
                latency_ms=int((time.monotonic() - started) * 1000),
                reason=(
                    "exact_output"
                    if exact
                    else "output_too_long"
                    if output_too_long
                    else "output_mismatch"
                ),
            )
    except Exception as exc:
        raise WorkerCanaryUnavailable("canary observation unavailable") from exc

    return _result(
        status="failed",
        worker_name=worker_name,
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        reason="timeout",
    )
