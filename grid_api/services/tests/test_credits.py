# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the dark-shipped credit metering path.

These exercise the no-DB branches of `charge_request` — the only ones the live
request path hits while GRID_CHARGING_ENABLED=0 — so they need no Postgres:

* dry-run (charging disabled): reports `would_charge`, never debits.
* free (unpriced model): 0, no account lookup.
* legacy (no account_id): 0, not chargeable.

The DB-backed credit/debit/balance paths are integration-tested separately
(they require a live session) and stay dark until charging is flipped on.
"""

import pytest

from grid_api.services import credits, pricing


PRICED_MODEL = "deepseek-v4-flash"  # in the price book → quote > 0


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
