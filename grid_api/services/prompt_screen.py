# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Log-only safety screening of admitted prompts.

Admitted text, image, video and audio prompts are scored by an external
moderation classifier for one configured category (sexual content involving
minors by default). This module only observes: it never delays, rejects or
alters a request, and it is not part of any money or authentication
transaction. Callers enqueue after admission; one bounded background worker
does the network call.

Data minimisation: prompt text leaves the process only to the configured
classifier endpoint, after credential redaction and tail truncation. It is
never logged, stored or alerted. Records carry a keyed opaque account
identifier, the path, the model and the score.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
from urllib.parse import urlparse

import httpx

from ..config import get_settings
from ..safe_logging import error_type, opaque_id
from . import alerts
from .sanitizer import sanitize

logger = logging.getLogger("grid_api.prompt_screen")

_SENTINEL = object()
_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_client: httpx.AsyncClient | None = None
_stats = {"scored": 0, "over": 0, "failed": 0, "dropped": 0}


def valid_endpoint(url: str) -> bool:
    """HTTPS anywhere; plain HTTP only to a literal loopback or private address."""
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def media_text(payload: dict | None) -> str:
    """The caller-written prompt strings of a media payload, nothing else.

    Negative prompts are excluded on purpose: naming a banned subject there is
    how callers steer away from it.
    """
    if not isinstance(payload, dict):
        return ""
    inputs = payload.get("recipe_inputs")
    sources = [payload, inputs if isinstance(inputs, dict) else {}]
    parts: list[str] = []
    for source in sources:
        for name in ("prompt", "local_prompts"):
            value = source.get(name)
            if isinstance(value, str) and value.strip() and value not in parts:
                parts.append(value)
    return "\n".join(parts)


def stats() -> dict[str, int]:
    return dict(_stats)


async def start() -> None:
    """Start the scoring worker. Safe, idempotent, and never fatal to startup."""
    global _queue, _worker_task, _client
    if _worker_task and not _worker_task.done():
        return
    settings = get_settings()
    if settings.prompt_screen_mode == "off":
        logger.info("Prompt screening is disabled")
        return
    secret = settings.prompt_screen_api_key
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        logger.error("PROMPT_SCREEN_MODE is set without PROMPT_SCREEN_API_KEY; screening disabled")
        return
    if not valid_endpoint(settings.prompt_screen_url):
        logger.error("PROMPT_SCREEN_URL is not an acceptable endpoint; screening disabled")
        return
    alerts._harden_transport_logging()
    _queue = asyncio.Queue(maxsize=settings.prompt_screen_queue_size)
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.prompt_screen_timeout_seconds),
        headers={"Authorization": f"Bearer {key}"},
        follow_redirects=False,
    )
    _worker_task = asyncio.create_task(_run(), name="grid-prompt-screen")
    logger.info("Prompt screening started in %s mode", settings.prompt_screen_mode)


def observe(path: str, model: str, text: str | None, account_id=None) -> bool:
    """Enqueue one admitted prompt without waiting. Never raises.

    Returns False when screening is off, the text is empty, or the queue is full.
    """
    try:
        if _queue is None or _worker_task is None or _worker_task.done():
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        limit = get_settings().prompt_screen_max_chars
        # Keep the tail: the newest turn carries the request being made.
        item = (str(path)[:40], str(model)[:120], text[-limit:], opaque_id(account_id))
        _queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        _stats["dropped"] += 1
        logger.warning("prompt_screen queue full; dropping one observation")
        return False
    except Exception as exc:
        logger.warning("prompt_screen observe failed error_type=%s", error_type(exc))
        return False


async def score(text: str) -> float:
    """Return the configured category score in [0, 1] or raise."""
    settings = get_settings()
    if _client is None:
        raise RuntimeError("prompt screen is not started")
    response = await _client.post(
        settings.prompt_screen_url,
        json={"model": settings.prompt_screen_model, "input": sanitize(text).text},
    )
    response.raise_for_status()
    value = response.json()["results"][0]["category_scores"][settings.prompt_screen_category]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("category score is not a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("category score is out of range")
    return value


async def _run() -> None:
    queue = _queue
    if queue is None:
        return
    threshold = get_settings().prompt_screen_threshold
    while True:
        item = await queue.get()
        try:
            if item is _SENTINEL:
                return
            path, model, text, account = item
            try:
                value = await score(text)
            except Exception as exc:
                _stats["failed"] += 1
                logger.warning("prompt_screen unavailable path=%s error_type=%s", path, error_type(exc))
                alerts.emit(
                    "safety_screen_unavailable",
                    "warning",
                    "The safety screening service did not return a score. Requests are unaffected (log-only).",
                    fields={"path": path, "error_type": error_type(exc)},
                    dedupe_key="safety-screen-unavailable",
                )
                continue
            over = value >= threshold
            _stats["scored"] += 1
            logger.info(
                "prompt_screen path=%s model=%s account=%s score=%.6f over=%d chars=%d",
                path, model, account, value, int(over), len(text),
            )
            if over:
                _stats["over"] += 1
                alerts.emit(
                    "safety_screen_hit",
                    "warning",
                    "A request scored over the safety threshold. It was not refused (log-only).",
                    fields={"path": path, "model": model, "account": account, "score": f"{value:.4f}"},
                    dedupe_key=f"safety-screen-hit:{account}",
                )
        except Exception as exc:
            logger.error("prompt_screen worker failed one item error_type=%s", error_type(exc))
        finally:
            queue.task_done()


async def flush(timeout: float = 5.0) -> None:
    if _queue is None:
        return
    try:
        await asyncio.wait_for(_queue.join(), timeout=timeout)
    except TimeoutError:
        logger.warning("Timed out draining prompt screen queue")


async def stop() -> None:
    """Drain briefly and close the transport. Undelivered observations are dropped."""
    global _queue, _worker_task, _client
    if _queue is not None and _worker_task is not None and not _worker_task.done():
        await flush(timeout=2.0)
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):
            pass
    if _client is not None:
        await _client.aclose()
    _queue = None
    _worker_task = None
    _client = None
