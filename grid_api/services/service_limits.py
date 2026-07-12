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
redis.call('SET', KEYS[2], amount, 'EX', tonumber(ARGV[3]))
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
