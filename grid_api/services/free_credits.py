# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Daily FREE credit allowance — the credit-denominated free tier.

Verified-human accounts get a daily allowance of micro-USD credits, reset at
UTC midnight (use-it-or-lose-it). AIPG-holding wallets can earn a separate
holder allowance without Google. A charge draws from this FREE bucket before the
purchased balance (see credits.charge_request). Tracked in Redis (per-account
-per-day key, auto-expiring) — no cleanup job, no unbounded growth.

HOLDER ALLOWANCE (default off). A login wallet qualifies for a tier by the AIPG
it held at the last Base block before today's UTC midnight AND still holds now
(the lower of the two decides the tier). Every wallet is measured at that one
block, so moving tokens between wallets cannot qualify two of them on the same
day. The first qualifying read of the day claims the allowance atomically in
Redis: one claim per wallet, one per account, and the sum of all claims stays
under GRID_FREE_HOLDER_DAILY_BUDGET_MICRO. Later reads that day use the claim
and touch no chain. Any failure (RPC, Redis, budget spent) grants nothing.

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
matching behavior. Enable it only alongside a reviewed
`GRID_CHARGING_MODE=allowlist` canary or the later global rollout.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import sqlalchemy as sa

from ..database import new_session
from ..redis_client import get_redis
from ..safe_logging import error_type, opaque_id
from ..v2.schema import account_identities, accounts

logger = logging.getLogger("grid_api.free_credits")

FREE_ENABLED = os.getenv("GRID_FREE_CREDITS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
# Whether the free bucket is actually consumed by the LIVE charge path yet. OFF
# until `consume()` is wired into authorize_request/authorize_media with durable
# reserve/release (see the PREVIEW note above). Surfaced as free.active so the API
# never implies free credit can cover a paid charge before that integration ships.
FREE_SPENDABLE_LIVE = os.getenv("GRID_FREE_SPENDABLE_LIVE", "0").lower() in ("1", "true", "yes", "on")
# Base free micro-USD per UTC day (10000 = $0.01).
FREE_DAILY_MICRO = int(os.getenv("GRID_FREE_DAILY_MICRO", "10000"))
# Holder allowance. Off unless at least one tier AND a positive network-wide
# daily budget are configured. A single tier may be given with the two legacy
# variables; GRID_FREE_HOLDER_TIERS ("min_aipg:micro_usd_per_day,...") wins.
FREE_HOLDER_MIN_AIPG = int(os.getenv("GRID_FREE_HOLDER_MIN_AIPG", "100000"))
FREE_HOLDER_BONUS_MICRO = int(os.getenv("GRID_FREE_HOLDER_BONUS_MICRO", "0"))
FREE_HOLDER_TIERS_RAW = os.getenv("GRID_FREE_HOLDER_TIERS", "").strip()
# Most holder allowance the whole network may claim in one UTC day.
FREE_HOLDER_DAILY_BUDGET_MICRO = int(os.getenv("GRID_FREE_HOLDER_DAILY_BUDGET_MICRO", "0"))
# Upper bound on the chain reads a first-of-day qualification may spend.
FREE_HOLDER_LOOKUP_SECONDS = float(os.getenv("GRID_FREE_HOLDER_LOOKUP_SECONDS", "8"))

_PREFIX = "grid:freecredit:"       # {_PREFIX}{account_id}:{day} -> micro spent today
_REF_PREFIX = "grid:freeconsumed:" # {_REF_PREFIX}{ref} -> micro consumed for that job (idempotency)
_HOLDER_ACCOUNT_PREFIX = "grid:holderbonus:"  # {..}{day}:{account_id} -> micro claimed today
_HOLDER_WALLET_PREFIX = "grid:holderwallet:"  # {..}{day}:{wallet} -> account_id that claimed
_HOLDER_BUDGET_PREFIX = "grid:holderbudget:"  # {..}{day} -> micro claimed network-wide
_HOLDER_BLOCK_PREFIX = "grid:holderblock:"    # {..}{day} -> snapshot block number


def parse_holder_tiers(raw: str) -> list[tuple[int, int]]:
    """Parse "min_aipg:micro,..." into tiers, largest holding first. Any invalid
    entry disables the whole list: a typo must never widen a giveaway."""
    tiers: dict[int, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            min_text, micro_text = part.split(":")
            min_aipg, micro = int(min_text), int(micro_text)
        except ValueError:
            logger.error("invalid GRID_FREE_HOLDER_TIERS; holder allowance disabled")
            return []
        if min_aipg <= 0 or micro <= 0 or min_aipg in tiers:
            logger.error("invalid GRID_FREE_HOLDER_TIERS; holder allowance disabled")
            return []
        tiers[min_aipg] = micro
    ordered = sorted(tiers.items(), reverse=True)
    if any(a[1] < b[1] for a, b in zip(ordered, ordered[1:])):
        logger.error("GRID_FREE_HOLDER_TIERS must not pay less for holding more; disabled")
        return []
    return ordered


def holder_tiers() -> list[tuple[int, int]]:
    """Active tiers as (whole AIPG held, micro-USD per day), largest first."""
    if FREE_HOLDER_DAILY_BUDGET_MICRO <= 0:
        return []
    if FREE_HOLDER_TIERS_RAW:
        return parse_holder_tiers(FREE_HOLDER_TIERS_RAW)
    if FREE_HOLDER_BONUS_MICRO > 0 and FREE_HOLDER_MIN_AIPG > 0:
        return [(FREE_HOLDER_MIN_AIPG, FREE_HOLDER_BONUS_MICRO)]
    return []


def _tier_micro(balance_raw: int, tiers: list[tuple[int, int]], decimals: int) -> int:
    for min_aipg, micro in tiers:
        if balance_raw >= min_aipg * (10 ** decimals):
            return micro
    return 0


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


def _day_start_ts() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


_block_searches: dict[str, asyncio.Task] = {}  # day -> the one in-flight search


async def _find_snapshot_block(r, day: str, key: str) -> int:
    from . import holdings

    try:
        block = await holdings.block_before(_day_start_ts())
        # NX: if two processes race, both read the same chain and agree anyway.
        await r.set(key, block, ex=_secs_to_midnight() + 3600, nx=True)
        return int(await r.get(key) or block)
    finally:
        _block_searches.pop(day, None)


async def _snapshot_block(r) -> int:
    """Today's snapshot block: the last Base block before UTC midnight. Shared
    through Redis so every process measures every wallet at the same block. The
    search runs once per day and is shielded, so a caller that gives up waiting
    does not throw the search away for the next caller."""
    day = _day()
    key = f"{_HOLDER_BLOCK_PREFIX}{day}"
    known = await r.get(key)
    if known:
        return int(known)
    search = _block_searches.get(day)
    if search is None:
        search = asyncio.ensure_future(_find_snapshot_block(r, day, key))
        _block_searches[day] = search
    return await asyncio.shield(search)


# Atomic claim of today's holder allowance. One claim per account and per wallet;
# the network-wide total never passes the budget. Returns the micro-USD this
# account holds for today (0 when the wallet or the budget is taken).
_HOLDER_CLAIM_LUA = """
local mine = redis.call('GET', KEYS[1])
if mine then return tonumber(mine) end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
local micro = tonumber(ARGV[2])
local used = tonumber(redis.call('GET', KEYS[3]) or '0')
if used + micro > tonumber(ARGV[3]) then return 0 end
redis.call('INCRBY', KEYS[3], micro)
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[4]))
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[4]))
redis.call('SET', KEYS[1], micro, 'EX', tonumber(ARGV[4]))
return micro
"""


async def _claim_holder_bonus(r, account_id, wallet: str, micro: int) -> int:
    day = _day()
    taken = await r.eval(
        _HOLDER_CLAIM_LUA, 3,
        f"{_HOLDER_ACCOUNT_PREFIX}{day}:{account_id}",
        f"{_HOLDER_WALLET_PREFIX}{day}:{wallet}",
        f"{_HOLDER_BUDGET_PREFIX}{day}",
        str(account_id), int(micro), FREE_HOLDER_DAILY_BUDGET_MICRO, _secs_to_midnight(),
    )
    return int(taken or 0)


async def _qualify_holder(r, account_id, wallet: str, tiers: list[tuple[int, int]]) -> int:
    from . import holdings

    now_micro = _tier_micro(await holdings.aipg_balance_raw(wallet), tiers, holdings.AIPG_DECIMALS)
    if now_micro <= 0:
        return 0
    block = await _snapshot_block(r)
    held = await holdings.aipg_balance_raw_at(wallet, block)
    micro = min(now_micro, _tier_micro(held, tiers, holdings.AIPG_DECIMALS))
    if micro <= 0:
        return 0
    return await _claim_holder_bonus(r, account_id, wallet, micro)


async def holder_bonus_micro(account_id, wallet: str | None) -> int:
    """Today's holder allowance for this account in micro-USD. 0 on any failure
    (fail-closed): an RPC or Redis outage must never grant free value."""
    if not FREE_ENABLED or not account_id:
        return 0
    tiers = holder_tiers()
    if not tiers:
        return 0
    try:
        r = get_redis()
        claimed = await r.get(f"{_HOLDER_ACCOUNT_PREFIX}{_day()}:{account_id}")
        if claimed is not None:
            return max(int(claimed), 0)
        if wallet is None:
            wallet = await _wallet_for_account(account_id)
        wallet = (wallet or "").strip().lower()
        if not (wallet.startswith("0x") and len(wallet) == 42):
            return 0
        return await asyncio.wait_for(
            _qualify_holder(r, account_id, wallet, tiers), FREE_HOLDER_LOOKUP_SECONDS
        )
    except Exception as exc:
        logger.warning(
            "holder allowance lookup failed; granting none account=%s error_type=%s",
            opaque_id(account_id),
            error_type(exc),
        )
        return 0


async def daily_cap_micro(account_id, wallet: str | None) -> int:
    """Daily cap: verified-Google base plus the claimed holder allowance."""
    if not FREE_ENABLED:
        return 0
    cap = FREE_DAILY_MICRO if await _has_verified_google(account_id) else 0
    return cap + await holder_bonus_micro(account_id, wallet)


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
    except Exception as exc:
        logger.warning(
            "free credits consume failed (charging paid instead) account=%s error_type=%s",
            opaque_id(account_id),
            error_type(exc),
        )
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
    except Exception as exc:
        logger.warning(
            "free credits release failed account=%s ref=%s error_type=%s",
            opaque_id(account_id),
            opaque_id(ref),
            error_type(exc),
        )
        return 0
