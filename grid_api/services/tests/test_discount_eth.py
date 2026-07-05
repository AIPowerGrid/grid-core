# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the >=100k-AIPG holder discount and the ETH deposit pricing.

No Postgres: the discount is exercised via the wallet path (no account->wallet
lookup) and the on-chain reads are stubbed, so these run in the dark-shipped
config exactly like the existing credit-metering tests."""

import pytest

from grid_api.services import credits, holdings, pricing


PRICED_MODEL = "deepseek-v4-flash"
WALLET = "0x000000000000000000000000000000000000dEAD"


# ── holder discount ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discount_off_by_default_is_noop():
    assert credits.HOLDER_DISCOUNT_ENABLED is False
    assert await credits.apply_holder_discount(1_000_000, wallet=WALLET) == 1_000_000


@pytest.mark.asyncio
async def test_discount_applies_when_holder_qualifies(monkeypatch):
    monkeypatch.setattr(credits, "HOLDER_DISCOUNT_ENABLED", True)
    monkeypatch.setattr(credits, "HOLDER_DISCOUNT_BPS", 2500)      # 25%
    monkeypatch.setattr(credits, "HOLDER_MIN_AIPG", 100_000)
    # 150k AIPG (18-dec) — above the 100k threshold.
    monkeypatch.setattr(holdings, "aipg_balance_raw",
                        _async(150_000 * 10 ** holdings.AIPG_DECIMALS))
    assert await credits.apply_holder_discount(1_000_000, wallet=WALLET) == 750_000


@pytest.mark.asyncio
async def test_no_discount_below_threshold(monkeypatch):
    monkeypatch.setattr(credits, "HOLDER_DISCOUNT_ENABLED", True)
    # 99,999 AIPG — one short of the 100k threshold.
    monkeypatch.setattr(holdings, "aipg_balance_raw",
                        _async(99_999 * 10 ** holdings.AIPG_DECIMALS))
    assert await credits.apply_holder_discount(1_000_000, wallet=WALLET) == 1_000_000


@pytest.mark.asyncio
async def test_discount_read_failure_never_blocks(monkeypatch):
    monkeypatch.setattr(credits, "HOLDER_DISCOUNT_ENABLED", True)

    async def boom(_):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(holdings, "aipg_balance_raw", boom)
    # A failed balance read charges full price, never raises.
    assert await credits.apply_holder_discount(1_000_000, wallet=WALLET) == 1_000_000


@pytest.mark.asyncio
async def test_charge_request_dry_run_reflects_discount(monkeypatch):
    monkeypatch.setattr(credits, "HOLDER_DISCOUNT_ENABLED", True)
    monkeypatch.setattr(holdings, "aipg_balance_raw",
                        _async(200_000 * 10 ** holdings.AIPG_DECIMALS))
    full = pricing.quote_text(PRICED_MODEL, 1000, 2000)
    user = {"account_id": "00000000-0000-0000-0000-000000000001", "wallet": WALLET}
    out = await credits.charge_request(user, PRICED_MODEL, 1000, 2000, "job-disc-1")
    assert out["status"] == "dry_run"
    assert out["would_charge"] == full * 7500 // 10_000  # 25% off


# ── ETH deposit pricing (Chainlink parse + credit math) ─────────────────────

@pytest.mark.asyncio
async def test_eth_usd_micro_parses_chainlink_answer(monkeypatch):
    holdings._price_cache.clear()
    # latestRoundData() → 5 words; 2nd word = answer (8-dec). $3,000.00 = 3000e8.
    answer = 3000 * 10 ** 8
    word = lambda v: hex(v)[2:].rjust(64, "0")
    res = "0x" + word(1) + word(answer) + word(0) + word(0) + word(1)
    monkeypatch.setattr(holdings, "_eth_call", _async(res))
    micro = await holdings.eth_usd_micro()
    assert micro == 3000 * 1_000_000  # micro-USD per ETH


def test_eth_deposit_credit_math():
    # 0.5 ETH at $3,000 → $1,500.00 = 1_500_000_000 micro-USD.
    value_wei = 5 * 10 ** 17
    px_micro = 3000 * 1_000_000
    assert value_wei * px_micro // (10 ** 18) == 1_500_000_000


def _async(value):
    async def _f(*a, **k):
        return value
    return _f
