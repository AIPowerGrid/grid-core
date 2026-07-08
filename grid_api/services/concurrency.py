# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-account in-flight concurrency caps — a Redis counter that holds across all
uvicorn workers, namespaced by job kind so text and media caps are independent.

Bounds how many concurrent requests one account can have in flight, so a single
account can't monopolize worker slots + token-stream pubsub subscriptions with a
flood of long-running jobs (media already had this; this generalizes it for text).

FAIL-OPEN: this is availability hardening, not money. A Redis blip must never 402
or 503 legitimate traffic — on any store error `acquire` allows the request and
`release` is a best-effort no-op. The counter self-heals via a TTL (a leaked slot
from a crash expires) and a floor-at-zero on release."""

import logging

logger = logging.getLogger("grid_api.concurrency")

_PREFIX = "grid:inflight:"
_TTL = 1800  # safety: a job longer than this is dead anyway; leaked slots self-heal


def _key(kind: str, account_id) -> str:
    return f"{_PREFIX}{kind}:{account_id}"


async def acquire(account_id, kind: str, limit: int) -> bool:
    """Reserve one in-flight slot for (account, kind). Returns False (reserving
    nothing) only when the account is genuinely at its limit; True on no cap, no
    account, or any Redis error (fail-open)."""
    if not account_id or not limit or limit <= 0:
        return True
    try:
        from ..redis_client import get_redis
        r = get_redis()
        key = _key(kind, account_id)
        cur = await r.incr(key)
        await r.expire(key, _TTL)
        if cur > limit:
            await r.decr(key)
            return False
        return True
    except Exception as e:
        logger.debug("inflight acquire failed (fail-open) account=%s kind=%s: %s", account_id, kind, e)
        return True


async def release(account_id, kind: str) -> None:
    """Release one in-flight slot. Best-effort + floor-at-zero (a double release
    never drives the counter negative). Never raises."""
    if not account_id:
        return
    try:
        from ..redis_client import get_redis
        r = get_redis()
        key = _key(kind, account_id)
        if await r.decr(key) < 0:
            await r.set(key, 0)
    except Exception as e:
        logger.debug("inflight release failed account=%s kind=%s: %s", account_id, kind, e)
