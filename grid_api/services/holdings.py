# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""On-chain reads for the credits system (Base) — balances + a deposit-time price.

Two read-only helpers, both cached in-process:

- ``aipg_balance_raw(wallet)`` — the account's AIPG ERC-20 balance (raw base units).
  Feeds the ">=100k AIPG holder" charge discount. Stale-on-error (a perk must never
  block a charge), cached ~10 min so it adds no per-request RPC under load.

- ``block_before(ts)`` / ``aipg_balance_raw_at(wallet, block)`` — the balance at one
  fixed past block, for the holder daily allowance. Every wallet is read at the same
  block, so the same tokens cannot qualify two wallets on one day. Both RAISE on
  failure (free value must never be granted on a guessed balance) and need an RPC
  that serves state at least one day old.

- ``eth_usd_micro()`` — Chainlink ETH/USD on Base, as **micro-USD per 1 ETH**. Prices
  a native-ETH deposit at claim time. This is a DEPOSIT-time oracle only; the request
  path stays deliberately oracle-free (USDC/USD-native). Raises on failure — a deposit
  must never credit at a guessed price. Cached ~60 s.

All reads go through ``GRID_BASE_RPC`` (shared with the deposit watcher).
"""

import logging
import os
import time

import httpx

from ..safe_logging import error_type, opaque_id

logger = logging.getLogger("grid_api.holdings")

BASE_RPC = os.getenv("GRID_BASE_RPC", "https://mainnet.base.org").strip()

# AIPG ERC-20 on Base (18 decimals).
AIPG_TOKEN = os.getenv("GRID_AIPG_TOKEN", "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608").strip().lower()
AIPG_DECIMALS = int(os.getenv("GRID_AIPG_DECIMALS", "18") or 18)

# Chainlink ETH/USD aggregator on Base mainnet (8 decimals). Overridable for testnet.
ETH_USD_FEED = os.getenv("GRID_ETH_USD_FEED", "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70").strip().lower()
ETH_USD_DECIMALS = int(os.getenv("GRID_ETH_USD_FEED_DECIMALS", "8") or 8)

_BAL_TTL = int(os.getenv("GRID_HOLDINGS_TTL", "600") or 600)    # AIPG-balance cache seconds
_PRICE_TTL = int(os.getenv("GRID_ETH_PRICE_TTL", "60") or 60)   # ETH-price cache seconds

_bal_cache: dict[str, tuple[float, int]] = {}    # wallet -> (expiry_monotonic, raw_balance)
_price_cache: dict[str, tuple[float, int]] = {}  # "eth" -> (expiry_monotonic, micro_usd)


async def _rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body.get("result")


async def _eth_call(to: str, data: str, block: str = "latest") -> str:
    return await _rpc("eth_call", [{"to": to, "data": data}, block]) or "0x"


_SNAPSHOT_CACHE_MAX = 20_000
_snapshot_cache: dict[tuple[str, int], int] = {}  # (wallet, block) -> raw balance (immutable)
_block_cache: dict[int, int] = {}                 # unix ts -> last block before it


async def _block_time(number: int | str) -> tuple[int, int]:
    tag = number if isinstance(number, str) else hex(number)
    block = await _rpc("eth_getBlockByNumber", [tag, False])
    if not block:
        raise RuntimeError("block not found")
    return int(block["number"], 16), int(block["timestamp"], 16)


async def block_before(ts: int) -> int:
    """Number of the last Base block with a timestamp before `ts`. RAISES on any
    RPC failure or when `ts` is not in the chain's past."""
    hit = _block_cache.get(ts)
    if hit is not None:
        return hit
    high, high_ts = await _block_time("latest")
    if high_ts < ts:
        raise RuntimeError("snapshot time is not in the chain's past")
    low = 0  # invariant: block `low` is before ts, block `high` is not
    while high - low > 1:
        mid = (low + high) // 2
        _, mid_ts = await _block_time(mid)
        if mid_ts < ts:
            low = mid
        else:
            high = mid
    if low <= 0:
        raise RuntimeError("snapshot time is before the chain")
    if len(_block_cache) > 64:
        _block_cache.clear()
    _block_cache[ts] = low
    return low


async def aipg_balance_raw_at(wallet: str, block: int) -> int:
    """AIPG balance (raw base units) of `wallet` at the end of `block`. A past
    block never changes, so results are cached for the life of the process.
    RAISES on failure — callers must grant nothing on an unknown balance."""
    w = (wallet or "").strip().lower()
    if not (w.startswith("0x") and len(w) == 42) or block <= 0:
        raise ValueError("invalid wallet or block")
    key = (w, int(block))
    hit = _snapshot_cache.get(key)
    if hit is not None:
        return hit
    data = "0x70a08231" + w[2:].rjust(64, "0")  # balanceOf(address)
    res = await _eth_call(AIPG_TOKEN, data, hex(int(block)))
    if not res or res == "0x":
        raise RuntimeError("empty balance response")
    bal = int(res, 16)
    if len(_snapshot_cache) >= _SNAPSHOT_CACHE_MAX:
        _snapshot_cache.clear()
    _snapshot_cache[key] = bal
    return bal


async def aipg_balance_raw(wallet: str) -> int:
    """AIPG balance (raw base units) for a wallet. Cached ~10 min. Returns 0 on a
    first-time failure; serves the last known value on a later failure (stale-ok —
    the discount is a perk, never a gate)."""
    w = (wallet or "").strip().lower()
    if not (w.startswith("0x") and len(w) == 42):
        return 0
    now = time.monotonic()
    hit = _bal_cache.get(w)
    if hit and hit[0] > now:
        return hit[1]
    try:
        data = "0x70a08231" + w[2:].rjust(64, "0")  # balanceOf(address)
        res = await _eth_call(AIPG_TOKEN, data)
        bal = int(res, 16) if res and res != "0x" else 0
    except Exception as exc:
        logger.warning(
            "aipg balance read failed wallet=%s error_type=%s",
            opaque_id(w),
            error_type(exc),
        )
        return hit[1] if hit else 0
    _bal_cache[w] = (now + _BAL_TTL, bal)
    return bal


async def eth_usd_micro() -> int:
    """Chainlink ETH/USD on Base → micro-USD per 1 ETH. Cached ~60 s. RAISES on
    failure (a deposit must never credit at a guessed price)."""
    now = time.monotonic()
    hit = _price_cache.get("eth")
    if hit and hit[0] > now:
        return hit[1]
    # latestRoundData() -> (roundId, int256 answer, startedAt, updatedAt, answeredInRound)
    res = await _eth_call(ETH_USD_FEED, "0xfeaf968c")
    if not res or len(res) < 2 + 64 * 2:
        raise RuntimeError("bad ETH/USD feed response")
    answer = int(res[2 + 64: 2 + 128], 16)  # 2nd 32-byte word
    if answer <= 0:
        raise RuntimeError("non-positive ETH/USD answer")
    micro = answer * 1_000_000 // (10 ** ETH_USD_DECIMALS)  # micro-USD per ETH
    if micro <= 0:
        raise RuntimeError("ETH/USD underflow")
    _price_cache["eth"] = (now + _PRICE_TTL, micro)
    return micro
