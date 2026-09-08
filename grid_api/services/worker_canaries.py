# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Economically inert, hard-targeted worker connectivity canaries.

These canaries prove that Core can route a synthetic request through one exact
registered worker and receive a randomized text response or verified media
output. They are setup evidence only: they do not prove model identity,
intelligence, or quality.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any
from uuid import uuid4

from ..redis_client import get_redis
from . import audio, job_queue, recipes, token_stream

CANARY_COOLDOWN_SECONDS = 300
CANARY_TIMEOUT_SECONDS = 180
MEDIA_CANARY_TIMEOUT_SECONDS = 900
# Reasoning models spend completion tokens before producing the short answer.
CANARY_MAX_COMPLETION_TOKENS = 512
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


def _media_result(
    *,
    status: str,
    worker_name: str,
    model: str,
    job_type: str,
    latency_ms: int | None,
    reason: str,
) -> dict[str, Any]:
    result = _result(
        status=status,
        worker_name=worker_name,
        model=model,
        latency_ms=latency_ms,
        reason=reason,
    )
    result["proof_scope"] = "hard_targeted_connectivity_and_media_output"
    result["modality"] = job_type
    return result


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
    prompt = (
        "This is a connectivity check using a public, randomly generated test label. "
        f"Reply with the label only, without commentary: {expected}"
    )
    job_id = str(uuid4())
    payload = {
        "request": {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": CANARY_MAX_COMPLETION_TOKENS,
            "temperature": 0,
            "stream": True,
        },
        "api_format": "openai-chat",
        "prompt": prompt,
        "max_length": CANARY_MAX_COMPLETION_TOKENS,
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
            exact = not output_too_long and secrets.compare_digest(
                output.encode("utf-8"), expected.encode("utf-8"),
            )
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
                    else "output_budget_exhausted"
                    if event.get("finish_reason") == "length"
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


def _media_payload(
    models: list[str],
    job_type: str,
) -> tuple[dict[str, Any], list[str], str]:
    """Build the smallest governed payload that can exercise one media worker."""
    served_models = {item for item in models if isinstance(item, str) and item}
    if not served_models:
        raise WorkerCanaryUnavailable("worker advertises no media model")
    seed = secrets.randbelow(2**31)
    if job_type == "audio":
        model = sorted(served_models)[0]
        return (
            {
                "prompt": f"instrumental connectivity pulse {secrets.token_hex(6)}",
                "lyrics": "",
                "seconds": audio.MIN_AUDIO_SECONDS,
                "inference_steps": audio.MIN_INFERENCE_STEPS,
                "n": 1,
                "ext": "wav",
                "seed": seed,
                "seeds": [seed],
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
            },
            [model],
            model,
        )

    if job_type not in {"image", "video"}:
        raise WorkerCanaryUnavailable("worker has no supported media canary modality")
    candidates = [
        recipe
        for recipe in recipes.list_recipes()
        if recipe.job_type == job_type
        and "image" not in recipe.vars
        and set(recipe.required_models or [recipe.model_name]).issubset(served_models)
    ]
    if not candidates:
        raise WorkerCanaryUnavailable("worker model has no governed setup canary recipe")
    recipe = sorted(candidates, key=lambda item: item.recipe_root)[0]
    inputs: dict[str, Any] = {
        "prompt": f"simple geometric connectivity marker {secrets.token_hex(6)}",
        "negative_prompt": "text, watermark",
        "seed": seed,
    }
    # Use the recipe's admitted lower bounds to keep setup work deliberately small.
    for name in ("width", "height", "steps", "seconds", "fps"):
        bounds = recipe.clamps.get(name)
        if isinstance(bounds, list) and len(bounds) == 2:
            inputs[name] = bounds[0]
    resolved = recipes.resolve(recipe.recipe_root, inputs)
    ext = "mp4" if job_type == "video" else "webp"
    payload = {
        **inputs,
        "n": 1,
        "ext": ext,
        "recipe_engine": resolved["engine"],
        "recipe_spec": resolved["spec"],
        "recipe_root": resolved["recipe_root"],
        "recipe_id": resolved["recipe_id"],
        "deterministic": bool(resolved["deterministic"]),
    }
    return (
        payload,
        list(resolved.get("required_models") or [recipe.model_name]),
        recipe.model_name,
    )


async def run_media_connectivity_canary(
    *,
    worker_id: str,
    worker_name: str,
    models: list[str],
    job_type: str,
) -> dict[str, Any]:
    """Run one no-fallback media round trip without economic or validator effects."""
    await _acquire_canary_slot(worker_id)
    canary_id = f"self_{uuid4().hex}"
    grid_nonce = secrets.token_urlsafe(24)
    payload, route_models, tested_model = _media_payload(models, job_type)
    payload.update(
        {
            "_worker_self_canary": True,
            "_worker_self_canary_id": canary_id,
            "_worker_self_canary_nonce": grid_nonce,
        },
    )
    job_id = str(uuid4())
    started = time.monotonic()
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            route_models,
            job_type=job_type,
            preferred_worker=worker_name,
            hard_target_worker=worker_name,
        )
    except Exception as exc:
        raise WorkerCanaryUnavailable("canary dispatch unavailable") from exc

    try:
        async for event in token_stream.subscribe_tokens(
            job_id,
            timeout=MEDIA_CANARY_TIMEOUT_SECONDS,
        ):
            if event.get("error"):
                return _media_result(
                    status="failed",
                    worker_name=worker_name,
                    model=tested_model,
                    job_type=job_type,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    reason="worker_or_transport_error",
                )
            if event.get("text") != token_stream.DONE_SENTINEL:
                continue
            grid = event.get("grid") if isinstance(event.get("grid"), dict) else {}
            if (
                str(grid.get("worker_id") or "") != worker_id
                or str(grid.get("canary_id") or "") != canary_id
                or str(grid.get("grid_nonce") or "") != grid_nonce
                or grid.get("economic_effect") != "none"
            ):
                reason = "binding_mismatch"
            else:
                try:
                    body = json.loads(event.get("full_text") or "{}")
                    digest = str(body["output_sha256"])
                    content_type = str(body["content_type"])
                    expected_type = {
                        "image": "image/",
                        "video": "video/",
                        "audio": "audio/",
                    }[job_type]
                    valid = (
                        len(digest) == 64
                        and all(char in "0123456789abcdef" for char in digest)
                        and content_type.startswith(expected_type)
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    valid = False
                reason = "verified_media_output" if valid else "malformed_output"
            return _media_result(
                status="passed" if reason == "verified_media_output" else "failed",
                worker_name=worker_name,
                model=tested_model,
                job_type=job_type,
                latency_ms=int((time.monotonic() - started) * 1000),
                reason=reason,
            )
    except Exception as exc:
        raise WorkerCanaryUnavailable("canary observation unavailable") from exc

    return _media_result(
        status="failed",
        worker_name=worker_name,
        model=tested_model,
        job_type=job_type,
        latency_ms=int((time.monotonic() - started) * 1000),
        reason="timeout",
    )
