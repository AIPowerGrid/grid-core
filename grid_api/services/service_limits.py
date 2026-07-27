# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail-closed exposure ceilings for service-owned and delegated workloads."""

from __future__ import annotations

from datetime import datetime, timezone

from .service_auth import record_event

_LUA = """
local prior = redis.call('GET', KEYS[2])
if prior then return 1 end
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
if cap > 0 and used + amount > cap then return 0 end
redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
redis.call('SET', KEYS[2], ARGV[4] .. ':' .. ARGV[1], 'EX', tonumber(ARGV[3]))
return 1
"""

_RELEASE_LUA = """
local prior = redis.call('GET', KEYS[2])
if not prior or prior ~= ARGV[1] then return 0 end
local amount = tonumber(ARGV[2])
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
redis.call('SET', KEYS[1], math.max(used - amount, 0), 'KEEPTTL')
redis.call('DEL', KEYS[2])
return 1
"""

_RECONCILE_LUA = """
local prior = redis.call('GET', KEYS[2])
if not prior or prior ~= ARGV[1] then return 0 end
local reserved = tonumber(ARGV[2])
local keep = math.min(math.max(tonumber(ARGV[3]), 0), reserved)
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
redis.call('SET', KEYS[1], math.max(used - (reserved - keep), 0), 'KEEPTTL')
redis.call('SET', KEYS[2], ARGV[4] .. ':' .. keep, 'KEEPTTL')
return 1
"""


def _seconds_to_tomorrow() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
    return max(int(tomorrow - now.timestamp()) + 3600, 1)


async def authorize(user: dict, amount_micro: int, ref: str) -> tuple[bool, str | None]:
    service_id = user.get("service_id")
    limits = user.get("service_limits") or {}
    if not service_id:
        return True, None
    per_request = limits.get("per_request_micro")
    daily = limits.get("daily_micro")
    if per_request is not None and amount_micro > int(per_request):
        await record_event(
            service_id,
            "request_limit_rejected",
            account_id=user.get("account_id"),
            ref=f"limit-request:{service_id}:{ref}",
            metadata={"amount_micro": amount_micro},
        )
        return False, "service per-request spending ceiling exceeded"
    if daily is None:
        return True, None
    try:
        from ..redis_client import get_redis

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ok = await get_redis().eval(
            _LUA,
            2,
            f"grid:service-spend:{service_id}:{day}",
            f"grid:service-spend-ref:{service_id}:{ref}",
            int(amount_micro),
            int(daily),
            _seconds_to_tomorrow(),
            day,
        )
    except Exception:
        return False, "service spending ceiling unavailable"
    if not ok:
        await record_event(
            service_id,
            "daily_limit_rejected",
            account_id=user.get("account_id"),
            ref=f"limit-daily:{service_id}:{ref}",
            metadata={"amount_micro": amount_micro},
        )
        return False, "service daily spending ceiling exceeded"
    return True, None


def _parse_reservation(raw, fallback_day: str) -> tuple[str, int] | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    value = str(raw)
    if ":" in value:
        day, amount = value.rsplit(":", 1)
    else:
        # Compatibility with reservations written before day binding.
        day, amount = fallback_day, value
    try:
        return day, max(int(amount), 0)
    except ValueError:
        return None


async def release(service_id: str | None, ref: str) -> bool:
    """Release a service exposure reservation exactly once.

    Failure is conservative: the stale reservation expires with its UTC-day
    bucket instead of accidentally granting more service spend.
    """
    if not service_id:
        return False
    try:
        from ..redis_client import get_redis

        redis = get_redis()
        ref_key = f"grid:service-spend-ref:{service_id}:{ref}"
        fallback_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = await redis.get(ref_key)
        parsed = _parse_reservation(raw, fallback_day)
        if not parsed:
            return False
        day, amount = parsed
        return bool(await redis.eval(
            _RELEASE_LUA,
            2,
            f"grid:service-spend:{service_id}:{day}",
            ref_key,
            raw.decode() if isinstance(raw, bytes) else str(raw),
            amount,
        ))
    except Exception:
        return False


async def reconcile(service_id: str | None, ref: str, keep_micro: int) -> bool:
    """Reduce a service reservation to actual spend, idempotently."""
    if not service_id:
        return False
    try:
        from ..redis_client import get_redis

        redis = get_redis()
        ref_key = f"grid:service-spend-ref:{service_id}:{ref}"
        fallback_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = await redis.get(ref_key)
        parsed = _parse_reservation(raw, fallback_day)
        if not parsed:
            return False
        day, reserved = parsed
        keep = min(max(int(keep_micro), 0), reserved)
        expected = raw.decode() if isinstance(raw, bytes) else str(raw)
        return bool(await redis.eval(
            _RECONCILE_LUA,
            2,
            f"grid:service-spend:{service_id}:{day}",
            ref_key,
            expected,
            reserved,
            keep,
            day,
        ))
    except Exception:
        return False
