# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Daily FREE credit allowance — the credit-denominated free tier.

"Free AI for everyone, funded by paid usage." Every account gets a daily
allowance of micro-USD credits, reset at UTC midnight (use-it-or-lose-it),
tiered by AIPG held on Base. A charge draws from this FREE bucket BEFORE the
purchased balance (see credits.charge_request). Tracked in Redis (per-account
-per-day key, auto-expiring) — no cleanup job, no unbounded growth.

This complements the request-count `quota.py` (N free requests/day): that caps
how OFTEN you call; this caps how much VALUE you consume for free, so a cheap
chat and an expensive video draw the right amount from the same allowance.

FAIL-CLOSED (unlike quota.py, which fails open): if Redis is unavailable, free
availability reads as 0 and charges fall through to the paid balance. A free
CREDIT is real value — a store outage must never silently grant unbounded free
credits. (Letting a few extra *requests* through on a quota outage is cheap;
granting unbounded *credits* is not.)
"""

import logging
import os
from datetime import datetime, timezone

from ..redis_client import get_redis

logger = logging.getLogger("grid_api.free_credits")

FREE_ENABLED = os.getenv("GRID_FREE_CREDITS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
# Base free micro-USD per UTC day (250000 = $0.25).
FREE_DAILY_MICRO = int(os.getenv("GRID_FREE_DAILY_MICRO", "250000"))
# AIPG-holder bonus: wallets holding >= MIN get + BONUS micro-USD/day on top of base.
FREE_HOLDER_MIN_AIPG = int(os.getenv("GRID_FREE_HOLDER_MIN_AIPG", "100000"))
FREE_HOLDER_BONUS_MICRO = int(os.getenv("GRID_FREE_HOLDER_BONUS_MICRO", "1000000"))  # +$1.00/day

_PREFIX = "grid:freecredit:"       # {_PREFIX}{account_id}:{day} -> micro spent today
_REF_PREFIX = "grid:freeconsumed:" # {_REF_PREFIX}{ref} -> micro consumed for that job (idempotency)


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _secs_to_midnight() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(86400 - int((now - start).total_seconds()), 1)


async def daily_cap_micro(wallet: str | None) -> int:
    """Free micro-USD/day for this account: base + AIPG-holder bonus (read on Base)."""
    if not FREE_ENABLED:
        return 0
    cap = FREE_DAILY_MICRO
    if wallet and FREE_HOLDER_BONUS_MICRO > 0:
        try:
            from . import holdings
            bal = await holdings.aipg_balance_raw(wallet)
            if bal >= FREE_HOLDER_MIN_AIPG * (10 ** holdings.AIPG_DECIMALS):
                cap += FREE_HOLDER_BONUS_MICRO
        except Exception:
            logger.debug("holder-bonus read failed; base cap only", exc_info=True)
    return cap


async def _spent_today_micro(account_id) -> int:
    try:
        r = get_redis()
        return int(await r.get(f"{_PREFIX}{account_id}:{_day()}") or 0)
    except Exception:
        return 0  # fail-closed: unknown spend → treat as fully spent below


async def available_micro(account_id, wallet: str | None) -> int:
    """Free micro-USD left today (cap - spent). 0 on any failure (fail-closed).
    Read-only — for previews, the balance endpoint, and dry-run logging."""
    if not FREE_ENABLED or not account_id:
        return 0
    cap = await daily_cap_micro(wallet)
    if cap <= 0:
        return 0
    try:
        r = get_redis()
        spent = int(await r.get(f"{_PREFIX}{account_id}:{_day()}") or 0)
    except Exception:
        return 0
    return max(cap - spent, 0)


# Atomic "consume up to remaining under cap", idempotent on ref (KEYS[2]).
_CONSUME_LUA = """
local prior = redis.call('GET', KEYS[2])
if prior then return tonumber(prior) end
local cap = tonumber(ARGV[1])
local want = tonumber(ARGV[2])
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
local remaining = cap - spent
if remaining < 0 then remaining = 0 end
local take = want
if take > remaining then take = remaining end
if take < 0 then take = 0 end
if take > 0 then
  redis.call('INCRBY', KEYS[1], take)
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
end
redis.call('SET', KEYS[2], take, 'EX', tonumber(ARGV[4]))
return take
"""


async def consume(account_id, wallet: str | None, want_micro: int, ref: str) -> int:
    """Consume up to `want_micro` from today's free allowance. Atomic + idempotent
    on `ref` (a retried job returns the same amount, never double-consumes).
    Returns micro-USD taken from free. 0 on failure → caller charges paid balance."""
    if not FREE_ENABLED or not account_id or want_micro <= 0 or not ref:
        return 0
    cap = await daily_cap_micro(wallet)
    if cap <= 0:
        return 0
    try:
        r = get_redis()
        day_key = f"{_PREFIX}{account_id}:{_day()}"
        ref_key = f"{_REF_PREFIX}{ref}"
        taken = await r.eval(
            _CONSUME_LUA, 2, day_key, ref_key,
            cap, int(want_micro), _secs_to_midnight(), 3600,
        )
        return int(taken or 0)
    except Exception as e:
        logger.warning("free_credits consume failed (charging paid instead) account=%s: %s", account_id, e)
        return 0
