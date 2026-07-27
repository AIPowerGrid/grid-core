# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the dark-shipped credit metering path.

These exercise the no-DB branches of `charge_request` — the only ones the live
request path hits while GRID_CHARGING_MODE=off — so they need no Postgres:

* dry-run (charging disabled): reports `would_charge`, never debits.
* free (unpriced model): 0, no account lookup.
* legacy (no account_id): 0, not chargeable.

The DB-backed credit/debit/balance paths are integration-tested separately
(they require a live session) and stay dark until charging is flipped on.
"""

import pytest

from grid_api.services import credits, pricing


PRICED_MODEL = "deepseek-v4-flash"  # in the price book → quote > 0


def test_charging_policy_modes(monkeypatch):
    user = {"account_id": "A-1", "service_id": "Gallery"}

    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    assert credits.charging_enabled_for(user, PRICED_MODEL) is False

    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    assert credits.charging_enabled_for({}, PRICED_MODEL) is True

    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "allowlist")
    monkeypatch.setattr(credits, "CHARGING_ALLOW_ACCOUNTS", frozenset({"a-1"}))
    monkeypatch.setattr(credits, "CHARGING_ALLOW_SERVICES", frozenset())
    monkeypatch.setattr(credits, "CHARGING_ALLOW_MODELS", frozenset())
    assert credits.charging_enabled_for(user, PRICED_MODEL) is True
    assert credits.charging_enabled_for({"account_id": "a-2"}, PRICED_MODEL) is False

    monkeypatch.setattr(credits, "CHARGING_ALLOW_MODELS", frozenset({PRICED_MODEL}))
    assert credits.charging_enabled_for(user, PRICED_MODEL) is True
    assert credits.charging_enabled_for(user, "other-model") is False
    # Account status can report cohort membership without choosing a model.
    assert credits.charging_enabled_for(user) is True


def test_legacy_boolean_remains_emergency_compatible(monkeypatch):
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "")
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    assert credits.charging_mode() == "off"
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    assert credits.charging_mode() == "on"


def test_invalid_charging_mode_fails_closed(monkeypatch):
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "surprise")
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    assert credits.charging_mode() == "off"
    assert credits.charging_enabled_for({"account_id": "a-1"}, PRICED_MODEL) is False


@pytest.mark.asyncio
async def test_dry_run_reports_would_charge_without_debiting():
    # Charging is OFF by default — must not touch the DB, just log the quote.
    assert credits.CHARGING_ENABLED is False
    user = {"account_id": "00000000-0000-0000-0000-000000000001"}
    out = await credits.charge_request(user, PRICED_MODEL, 1000, 2000, "job-dry-1")
    assert out["status"] == "dry_run"
    assert out["charged"] == 0
    expected = pricing.quote_text(PRICED_MODEL, 1000, 2000)
    assert expected > 0
    assert out["would_charge"] == expected


@pytest.mark.asyncio
async def test_free_when_unpriced_model():
    user = {"account_id": "00000000-0000-0000-0000-000000000001"}
    out = await credits.charge_request(user, "no-such-model-xyz", 1000, 2000, "job-free-1")
    assert out == {"status": "free", "charged": 0}


@pytest.mark.asyncio
async def test_legacy_account_not_charged():
    # Legacy API keys have no account_id → not chargeable (even when priced).
    out = await credits.charge_request({}, PRICED_MODEL, 1000, 2000, "job-legacy-1")
    assert out == {"status": "legacy", "charged": 0}
