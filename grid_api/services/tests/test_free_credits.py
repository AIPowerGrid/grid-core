# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free daily credit allowance — cap tiering + free-first draw in charge_request.

No Redis: the daily-cap tiering is pure (mock the on-chain balance), and the
charge-path split is exercised by stubbing the allowance. The atomic Redis
consume (Lua) is covered by a separate real-Redis integration run."""

import pytest

from grid_api.services import credits, free_credits, holdings, pricing

PRICED = "deepseek-v4-flash"


def _async(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.mark.asyncio
async def test_daily_cap_base_no_wallet(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_DAILY_MICRO", 250_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(True))
    assert await free_credits.daily_cap_micro("account", None) == 250_000


@pytest.mark.asyncio
async def test_daily_cap_holder_gets_bonus(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_DAILY_MICRO", 250_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_MIN_AIPG", 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_BONUS_MICRO", 1_000_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(True))
    monkeypatch.setattr(holdings, "aipg_balance_raw", _async(150_000 * 10 ** holdings.AIPG_DECIMALS))
    assert await free_credits.daily_cap_micro("account", "0xabc") == 1_250_000  # base + bonus


@pytest.mark.asyncio
async def test_daily_cap_below_threshold_base_only(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_DAILY_MICRO", 250_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(True))
    monkeypatch.setattr(holdings, "aipg_balance_raw", _async(99_999 * 10 ** holdings.AIPG_DECIMALS))
    assert await free_credits.daily_cap_micro("account", "0xabc") == 250_000


@pytest.mark.asyncio
async def test_wallet_only_non_holder_gets_no_free_faucet(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(False))
    monkeypatch.setattr(holdings, "aipg_balance_raw", _async(0))
    assert await free_credits.daily_cap_micro("wallet-account", "0xabc") == 0


@pytest.mark.asyncio
async def test_wallet_only_holder_gets_holder_bonus(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_MIN_AIPG", 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_BONUS_MICRO", 200_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(False))
    monkeypatch.setattr(holdings, "aipg_balance_raw", _async(100_000 * 10 ** holdings.AIPG_DECIMALS))
    assert await free_credits.daily_cap_micro("wallet-account", "0xabc") == 200_000


@pytest.mark.asyncio
async def test_media_account_id_resolves_wallet_for_holder_bonus(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_MIN_AIPG", 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_BONUS_MICRO", 200_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(False))
    monkeypatch.setattr(free_credits, "_wallet_for_account", _async("0xabc"))
    monkeypatch.setattr(
        holdings, "aipg_balance_raw",
        _async(100_000 * 10 ** holdings.AIPG_DECIMALS),
    )
    assert await free_credits.daily_cap_micro("wallet-account", None) == 200_000


@pytest.mark.asyncio
async def test_daily_cap_disabled_is_zero(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", False)
    assert await free_credits.daily_cap_micro(None, None) == 0
    assert await free_credits.available_micro("acct", "0xabc") == 0


@pytest.mark.asyncio
async def test_charge_dry_run_reports_free_first_split(monkeypatch):
    # Free covers PART of the cost → dry-run reports the free/paid split.
    monkeypatch.setattr(free_credits, "available_micro", _async(200))
    full = pricing.quote_text(PRICED, 1000, 2000)
    assert full > 200
    user = {"account_id": "00000000-0000-0000-0000-000000000001", "wallet": "0xdead"}
    out = await credits.charge_request(user, PRICED, 1000, 2000, "job-free-1")
    assert out["status"] == "dry_run"
    assert out["would_charge"] == full
    assert out["from_free"] == 200
    assert out["from_paid"] == full - 200


@pytest.mark.asyncio
async def test_charge_dry_run_free_covers_everything(monkeypatch):
    full = pricing.quote_text(PRICED, 1000, 2000)
    monkeypatch.setattr(free_credits, "available_micro", _async(full + 10_000))
    user = {"account_id": "x", "wallet": "0xdead"}
    out = await credits.charge_request(user, PRICED, 1000, 2000, "job-free-2")
    assert out["from_free"] == full
    assert out["from_paid"] == 0
