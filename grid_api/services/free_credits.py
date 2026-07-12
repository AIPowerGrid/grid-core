# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Daily FREE credit allowance — the credit-denominated free tier.

Verified-human accounts get a daily allowance of micro-USD credits, reset at
UTC midnight (use-it-or-lose-it). AIPG-holding wallets can earn a separate
holder allowance without Google. A charge draws from this FREE bucket before the
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

LIVE-PATH INTEGRATION (done, flag-gated): `consume()` is wired into the durable
reserve path — `credits.authorize_request` / `authorize_media` draw free-first
and hold only the remainder from paid; the reservation row records the split
(`grid_reservations.free_micro`); settlement restores free-to-free (`release()`)
and refunds paid-to-paid — the pockets never convert. The whole draw is gated on
**GRID_FREE_SPENDABLE_LIVE** (default OFF): flag off → free is display-only and
`/v1/account/credits` reports free.active=false / total_spendable=paid, exactly
matching behavior. Flip it together with GRID_CHARGING_ENABLED at go-live.
"""

import logging
import os
from datetime import datetime, timezone

import sqlalchemy as sa

from ..database import new_session
from ..redis_client import get_redis
from ..v2.schema import account_identities, accounts

logger = logging.getLogger("grid_api.free_credits")

FREE_ENABLED = os.getenv("GRID_FREE_CREDITS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
# Whether the free bucket is actually consumed by the LIVE charge path yet. OFF
# until `consume()` is wired into authorize_request/authorize_media with durable
# reserve/release (see the PREVIEW note above). Surfaced as free.active so the API
# never implies free credit can cover a paid charge before that integration ships.
FREE_SPENDABLE_LIVE = os.getenv("GRID_FREE_SPENDABLE_LIVE", "0").lower() in ("1", "true", "yes", "on")
# Base free micro-USD per UTC day (50000 = $0.05).
FREE_DAILY_MICRO = int(os.getenv("GRID_FREE_DAILY_MICRO", "50000"))
# AIPG-holder bonus: wallets holding >= MIN get + BONUS micro-USD/day on top of base.
FREE_HOLDER_MIN_AIPG = int(os.getenv("GRID_FREE_HOLDER_MIN_AIPG", "100000"))
FREE_HOLDER_BONUS_MICRO = int(os.getenv("GRID_FREE_HOLDER_BONUS_MICRO", "200000"))  # +$0.20/day

_PREFIX = "grid:freecredit:"       # {_PREFIX}{account_id}:{day} -> micro spent today
_REF_PREFIX = "grid:freeconsumed:" # {_REF_PREFIX}{ref} -> micro consumed for that job (idempotency)


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _secs_to_midnight() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(86400 - int((now - start).total_seconds()), 1)


async def _has_verified_google(account_id) -> bool:
    if not account_id:
        return False
    try:
        async with await new_session() as session:
            return bool(await session.scalar(
                sa.select(sa.literal(True)).where(sa.exists(
                    sa.select(account_identities.c.id).where(
                        account_identities.c.account_id == account_id,
                        account_identities.c.kind == "google",
                        account_identities.c.verified_at.is_not(None),
                    )
                ))
            ))
    except Exception:
        return False


async def _wallet_for_account(account_id) -> str | None:
    """Resolve the primary login wallet for media paths that carry only an id."""
    if not account_id:
        return None
    try:
        async with await new_session() as session:
            return await session.scalar(
                sa.select(accounts.c.wallet).where(accounts.c.id == account_id)
            )
    except Exception:
        return None


async def daily_cap_micro(account_id, wallet: str | None) -> int:
    """Daily cap: verified-Google base plus an independent AIPG-holder bonus."""
    if not FREE_ENABLED:
        return 0
    cap = FREE_DAILY_MICRO if await _has_verified_google(account_id) else 0
    if wallet is None:
        wallet = await _wallet_for_account(account_id)
    if wallet and FREE_HOLDER_BONUS_MICRO > 0:
        try:
            from . import holdings
            bal = await holdings.aipg_balance_raw(wallet)
            if bal >= FREE_HOLDER_MIN_AIPG * (10 ** holdings.AIPG_DECIMALS):
                cap += FREE_HOLDER_BONUS_MICRO
        except Exception:
            logger.debug("holder-bonus read failed; verified base only", exc_info=True)
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
    cap = await daily_cap_micro(account_id, wallet)
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
    Returns micro-USD taken from free. 0 on failure → caller charges paid balance.

    The ref record lives until end-of-day (+1h) so a settlement/refund hours
    later can still `release()` it; releasing past midnight is moot anyway (the
    allowance already reset)."""
    if not FREE_ENABLED or not account_id or want_micro <= 0 or not ref:
        return 0
    cap = await daily_cap_micro(account_id, wallet)
    if cap <= 0:
        return 0
    try:
        r = get_redis()
        day_key = f"{_PREFIX}{account_id}:{_day()}"
        ref_key = f"{_REF_PREFIX}{ref}"
        ttl = _secs_to_midnight()
        taken = await r.eval(
            _CONSUME_LUA, 2, day_key, ref_key,
            cap, int(want_micro), ttl, ttl + 3600,
        )
        return int(taken or 0)
    except Exception as e:
        logger.warning("free_credits consume failed (charging paid instead) account=%s: %s", account_id, e)
        return 0


# Atomic "give back what `ref` consumed beyond keep_micro" — the free-bucket
# mirror of a paid refund. Idempotent: the ref record is rewritten to keep_micro,
# so a repeat release computes delta 0. Only decrements a day key that still
# exists (past midnight the allowance already reset — nothing to restore).
_RELEASE_LUA = """
local consumed = tonumber(redis.call('GET', KEYS[2]) or '0')
local keep = tonumber(ARGV[1])
local delta = consumed - keep
if delta <= 0 then return 0 end
if redis.call('EXISTS', KEYS[1]) == 1 then
  local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
  local newspent = spent - delta
  if newspent < 0 then newspent = 0 end
  redis.call('SET', KEYS[1], newspent, 'KEEPTTL')
end
redis.call('SET', KEYS[2], keep, 'EX', tonumber(ARGV[2]))
return delta
"""


async def release(account_id, ref: str, keep_micro: int = 0) -> int:
    """Release the free consumption recorded under `ref` back to today's
    allowance, keeping `keep_micro` consumed (0 = full release on failure;
    keep = the actually-spent portion on an under-run settle). Atomic +
    idempotent — a duplicate terminal releases nothing twice. Returns the
    micro-USD restored. Best-effort: 0 on any failure (worst case the user
    loses part of ONE day's free allowance, never paid money)."""
    if not FREE_ENABLED or not account_id or not ref:
        return 0
    try:
        r = get_redis()
        day_key = f"{_PREFIX}{account_id}:{_day()}"
        ref_key = f"{_REF_PREFIX}{ref}"
        released = await r.eval(
            _RELEASE_LUA, 2, day_key, ref_key,
            max(int(keep_micro), 0), _secs_to_midnight() + 3600,
        )
        return int(released or 0)
    except Exception as e:
        logger.warning("free_credits release failed account=%s ref=%s: %s", account_id, ref, e)
        return 0
