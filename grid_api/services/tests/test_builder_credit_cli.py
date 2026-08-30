# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from argparse import Namespace
from uuid import uuid4

import pytest

from grid_api.services import promotions
from scripts.grant_builder_credit import usd_to_micro, validate_policy


def _args(**overrides):
    values = {
        "account_id": str(uuid4()),
        "amount_usd": "10",
        "budget_usd": "500",
        "expires_days": "60",
        "campaign_id": "builder-2026-q3",
        "campaign_name": "AIPG builders 2026 Q3",
        "ref": "builder:github:example/project#1",
    }
    values.update(overrides)
    return Namespace(**values)


def test_builder_grant_policy_accepts_bounded_cohort():
    _account, amount, budget, expiry = validate_policy(_args())
    assert (amount, budget, expiry) == (10_000_000, 500_000_000, 60)


@pytest.mark.parametrize("amount", ["4.999999", "20.000001", "1.0000001", "NaN", "Infinity"])
def test_builder_grant_policy_rejects_unbounded_or_inexact_amount(amount):
    with pytest.raises(ValueError):
        validate_policy(_args(amount_usd=amount))


def test_builder_campaign_policy_rejects_open_ended_value():
    with pytest.raises(ValueError):
        validate_policy(_args(budget_usd="1000.000001"))
    with pytest.raises(ValueError):
        validate_policy(_args(expires_days="91"))
    with pytest.raises(ValueError):
        validate_policy(_args(campaign_id="public-faucet"))
    with pytest.raises(ValueError):
        validate_policy(_args(campaign_id="builder-" + "a" * 57))
    with pytest.raises(ValueError):
        validate_policy(_args(campaign_name=" "))
    with pytest.raises(ValueError):
        validate_policy(_args(ref="signup:anyone"))


def test_usd_conversion_is_integer_micro_usd():
    assert usd_to_micro("5", label="amount") == 5_000_000


def test_builder_apply_gate_requires_exact_campaign(monkeypatch):
    monkeypatch.setattr(promotions, "PROMO_ENABLED", True)
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", True)
    monkeypatch.setattr(
        promotions,
        "PROMO_SPENDABLE_CAMPAIGNS",
        frozenset({"builder-2026-q3"}),
    )
    assert promotions.campaign_spendable("builder-2026-q3") is True
    assert promotions.campaign_spendable("universal-welcome-v1") is False
