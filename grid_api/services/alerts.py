# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Best-effort operational alerts with strict data minimization.

Alerts are never part of a money or authentication transaction. Callers enqueue
small, pre-classified events; one bounded background worker delivers them to an
operator-owned Discord webhook. Prompts, responses, credentials, email addresses,
signatures, and raw exceptions are intentionally unsupported.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from ..config import get_settings
from ..safe_logging import opaque_id

logger = logging.getLogger("grid_api.alerts")

_SENTINEL = object()
_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_client: httpx.AsyncClient | None = None
_last_sent: dict[str, float] = {}
_suppressed: dict[str, int] = {}

_BLOCKED_FIELD_FRAGMENTS = {
    "authorization",
    "cookie",
    "email",
    "key",
    "message",
    "output",
    "password",
    "prompt",
    "secret",
    "signature",
    "token",
    "webhook",
}
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_WEBHOOK_RE = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I)
_BEARER_RE = re.compile(r"\b(?:Bearer\s+)?(?:sk|aipg|grid)[-_][A-Za-z0-9._-]{12,}\b", re.I)
_COLORS = {
    "info": 0x4EA1FF,
    "warning": 0xF5A524,
    "critical": 0xE5484D,
    "success": 0x30A46C,
}


def _safe_text(value, *, limit: int = 240) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = _WEBHOOK_RE.sub("[redacted-webhook]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    text = _BEARER_RE.sub("[redacted-credential]", text)
    return text[:limit] or "-"


def _safe_fields(fields: Mapping | None) -> list[dict[str, object]]:
    safe = []
    for raw_name, raw_value in (fields or {}).items():
        name = str(raw_name).strip().lower().replace(" ", "_")
        if not name or any(fragment in name for fragment in _BLOCKED_FIELD_FRAGMENTS):
            continue
        if raw_value is None:
            continue
        safe.append(
            {
                "name": _safe_text(name, limit=80),
                "value": _safe_text(raw_value),
                "inline": len(str(raw_value)) <= 48,
            },
        )
        if len(safe) == 12:
            break
    return safe


def build_payload(kind: str, severity: str, summary: str, fields: Mapping | None = None) -> dict:
    """Build a Discord payload containing only bounded, redacted metadata."""
    severity = severity if severity in _COLORS else "info"
    return {
        "username": "AIPG Grid Operations",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{severity.upper()}: {_safe_text(kind, limit=80)}",
                "description": _safe_text(summary, limit=500),
                "color": _COLORS[severity],
                "fields": _safe_fields(fields),
                "timestamp": datetime.now(UTC).isoformat(),
                "footer": {"text": "grid-core"},
            },
        ],
    }


def _valid_webhook(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"discord.com", "canary.discord.com", "ptb.discord.com"}
        and parsed.path.startswith("/api/webhooks/")
    )


def _configured_url() -> str:
    secret = get_settings().grid_alert_discord_webhook
    return secret.get_secret_value().strip() if secret else ""


def _harden_transport_logging() -> None:
    """Prevent HTTP client request logs from printing credential-bearing URLs."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def start() -> None:
    """Start the delivery worker. Safe and idempotent."""
    global _queue, _worker_task, _client
    _harden_transport_logging()
    if _worker_task and not _worker_task.done():
        return
    url = _configured_url()
    if not url:
        logger.info("Discord operational alerts are disabled")
        return
    if not _valid_webhook(url):
        logger.error("GRID_ALERT_DISCORD_WEBHOOK is not an official Discord webhook; alerts disabled")
        return
    settings = get_settings()
    _queue = asyncio.Queue(maxsize=max(10, settings.grid_alert_queue_size))
    _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    _worker_task = asyncio.create_task(_run(url), name="grid-discord-alerts")


def emit(
    kind: str,
    severity: str,
    summary: str,
    *,
    fields: Mapping | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Enqueue an alert without waiting. Returns False when disabled or full."""
    if _queue is None or _worker_task is None or _worker_task.done():
        return False
    item = (kind, severity, summary, dict(fields or {}), dedupe_key or kind)
    try:
        _queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        logger.error("Discord alert queue full; dropping event kind=%s", _safe_text(kind, limit=80))
        return False


async def _deliver(url: str, payload: dict) -> bool:
    client = _client
    if client is None:
        logger.error("Discord alert client is not initialized")
        return False
    for attempt in range(3):
        try:
            response = await client.post(url, json=payload)
            if response.status_code in {200, 204}:
                return True
            if response.status_code == 429:
                delay = min(float(response.headers.get("retry-after", "1") or 1), 5.0)
            elif response.status_code >= 500:
                delay = min(2**attempt, 4)
            else:
                logger.error("Discord alert rejected with HTTP %d", response.status_code)
                return False
        except (httpx.TimeoutException, httpx.NetworkError):
            delay = min(2**attempt, 4)
        if attempt < 2:
            await asyncio.sleep(delay)
    logger.error("Discord alert delivery failed after retries")
    return False


async def _claim_distributed(key: str, ttl_seconds: int):
    """Claim one cross-process delivery window in Redis.

    Returns a release tuple for the winner, ``None`` for another process's
    duplicate, and ``False`` when Redis is unavailable (local dedupe applies).
    """
    if ttl_seconds <= 0:
        return False
    try:
        from ..redis_client import get_redis

        redis = get_redis()
        redis_key = f"grid:alerts:dedupe:{opaque_id(key)}"
        token = secrets.token_hex(16)
        claimed = await redis.set(redis_key, token, ex=ttl_seconds, nx=True)
        return (redis, redis_key, token) if claimed else None
    except Exception:
        return False


async def _release_distributed(claim) -> None:
    if not claim:
        return
    redis, key, token = claim
    try:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1,
            key,
            token,
        )
    except Exception:
        logger.debug("Could not release failed Discord alert dedupe claim")


async def _run(url: str) -> None:
    settings = get_settings()
    dedupe_seconds = max(0, settings.grid_alert_dedupe_seconds)
    queue = _queue
    if queue is None:
        logger.error("Discord alert queue is not initialized")
        return
    while True:
        item = await queue.get()
        try:
            if item is _SENTINEL:
                return
            kind, severity, summary, fields, key = item
            now = time.monotonic()
            if dedupe_seconds and now - _last_sent.get(key, 0) < dedupe_seconds:
                _suppressed[key] = _suppressed.get(key, 0) + 1
                continue
            claim = await _claim_distributed(key, dedupe_seconds)
            if claim is None:
                _suppressed[key] = _suppressed.get(key, 0) + 1
                continue
            suppressed = _suppressed.pop(key, 0)
            if suppressed:
                fields["suppressed_since_last"] = suppressed
            delivered = await _deliver(url, build_payload(kind, severity, summary, fields))
            if delivered:
                _last_sent[key] = now
            else:
                await _release_distributed(claim)
            await asyncio.sleep(0.3)
        except Exception:
            logger.exception("Discord alert worker failed processing one event")
        finally:
            queue.task_done()


async def flush(timeout: float = 5.0) -> None:
    if _queue is None:
        return
    try:
        await asyncio.wait_for(_queue.join(), timeout=timeout)
    except TimeoutError:
        logger.warning("Timed out draining Discord alerts")


async def stop() -> None:
    """Drain queued alerts and close the transport."""
    global _queue, _worker_task, _client
    if _queue is not None and _worker_task is not None and not _worker_task.done():
        await flush()
        await _queue.put(_SENTINEL)
        await _worker_task
    if _client is not None:
        await _client.aclose()
    _queue = None
    _worker_task = None
    _client = None
