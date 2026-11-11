# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free daily credit allowance — cap tiering + free-first draw in charge_request.

No Redis: the daily-cap sum is pure, and the charge-path split is exercised by
stubbing the allowance. The holder allowance's snapshot, claim and budget rules
run against a real disposable Redis in test_holder_allowance_redis.py."""

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
async def test_daily_cap_adds_claimed_holder_allowance(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_DAILY_MICRO", 250_000)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(True))
    monkeypatch.setattr(free_credits, "holder_bonus_micro", _async(1_000_000))
    assert await free_credits.daily_cap_micro("account", "0xabc") == 1_250_000  # base + bonus


@pytest.mark.asyncio
async def test_wallet_only_holder_gets_holder_allowance_alone(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(False))
    monkeypatch.setattr(free_credits, "holder_bonus_micro", _async(200_000))
    assert await free_credits.daily_cap_micro("wallet-account", "0xabc") == 200_000


@pytest.mark.asyncio
@pytest.mark.parametrize("tiers,bonus,budget", [
    ("", 0, 10_000_000),             # no tier configured
    ("", 200_000, 0),                # legacy single tier but no network budget
    ("1000:50000", 0, 0),            # tiers but no network budget
    ("1000:5x", 0, 10_000_000),      # typo disables the whole list
    ("1000:90000,5000:50000", 0, 10_000_000),  # paying less for more is rejected
])
async def test_holder_allowance_off_never_reads_chain_or_redis(monkeypatch, tiers, bonus, budget):
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_TIERS_RAW", tiers)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_BONUS_MICRO", bonus)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_DAILY_BUDGET_MICRO", budget)
    monkeypatch.setattr(free_credits, "_has_verified_google", _async(False))

    def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled holder policy must not read the chain or Redis")

    monkeypatch.setattr(holdings, "aipg_balance_raw", unexpected)
    monkeypatch.setattr(holdings, "aipg_balance_raw_at", unexpected)
    monkeypatch.setattr(free_credits, "get_redis", unexpected)
    assert await free_credits.daily_cap_micro("wallet-account", "0xabc") == 0


def test_holder_tiers_parse_largest_first(monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_HOLDER_DAILY_BUDGET_MICRO", 1)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_TIERS_RAW", "1000:50000, 100000:500000,10000:200000")
    assert free_credits.holder_tiers() == [(100_000, 500_000), (10_000, 200_000), (1_000, 50_000)]
    monkeypatch.setattr(free_credits, "FREE_HOLDER_TIERS_RAW", "")
    monkeypatch.setattr(free_credits, "FREE_HOLDER_MIN_AIPG", 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_BONUS_MICRO", 200_000)
    assert free_credits.holder_tiers() == [(100_000, 200_000)]


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


@pytest.mark.asyncio
async def test_live_legacy_charge_respects_disabled_free_spending(monkeypatch):
    async def unexpected_consume(*_args, **_kwargs):
        raise AssertionError("disabled free pocket must not be consumed")

    async def paid_ok(*_args, **_kwargs):
        return "ok"

    full = pricing.quote_text(PRICED, 1000, 2000)
    monkeypatch.setattr(credits, "charging_enabled_for", lambda *_args: True)
    monkeypatch.setattr(credits, "apply_holder_discount", _async(full))
    monkeypatch.setattr(credits, "_promo_first", _async(0))
    monkeypatch.setattr(free_credits, "FREE_SPENDABLE_LIVE", False)
    monkeypatch.setattr(free_credits, "consume", unexpected_consume)
    monkeypatch.setattr(credits, "debit", paid_ok)

    out = await credits.charge_request(
        {"account_id": "00000000-0000-0000-0000-000000000001"},
        PRICED,
        1000,
        2000,
        "job-live-free-off",
    )
    assert out["status"] == "ok"
    assert out["from_free"] == 0
    assert out["from_paid"] == out["charged"] == full
