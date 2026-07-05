# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""On-chain reads for the credits system (Base) — balances + a deposit-time price.

Two read-only helpers, both cached in-process:

- ``aipg_balance_raw(wallet)`` — the account's AIPG ERC-20 balance (raw base units).
  Feeds the ">=100k AIPG holder" charge discount. Stale-on-error (a perk must never
  block a charge), cached ~10 min so it adds no per-request RPC under load.

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


async def _eth_call(to: str, data: str) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": to, "data": data}, "latest"]},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"eth_call {to}: {body['error']}")
        return body.get("result") or "0x"


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
    except Exception as e:
        logger.warning("aipg_balance read failed for %s: %s", w, e)
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
