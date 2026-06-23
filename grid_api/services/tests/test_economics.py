# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the revenue/payout split math (pure integer micro-USD).

The invariant that matters on the money path: splits are EXACT — the parts
always sum back to the whole, with no rounding dust escaping. These pin the
default 85/3/12 split and the worker USDC/AIPG currency split.
"""

import importlib

from grid_api.services import economics


def test_default_split_is_85_3_12():
    # 1.000000 USD gross.
    s = economics.split_revenue(1_000_000)
    assert s["protocol"] == 120_000   # 12%
    assert s["sentinel"] == 30_000    # 3%
    assert s["worker"] == 850_000     # 85% (remainder)


def test_split_sums_to_gross_exactly_even_with_odd_amounts():
    # Odd amount that doesn't divide cleanly — the remainder must absorb dust.
    for gross in (1, 7, 333_333, 1_000_001, 999_999_999):
        s = economics.split_revenue(gross)
        assert s["protocol"] + s["sentinel"] + s["worker"] == gross
        assert all(v >= 0 for v in s.values())


def test_worker_pool_matches_remainder():
    assert economics.worker_pool_micro(1_000_000) == 850_000
    assert economics.worker_pool_micro(0) == 0


def test_worker_payout_split_all_usdc_at_launch_default():
    # Launch default WORKER_AIPG_SHARE_BPS=0 → everything in USDC.
    p = economics.split_worker_payout(500_000)
    assert p["usdc"] == 500_000 and p["aipg_value"] == 0
    assert p["usdc"] + p["aipg_value"] == 500_000


def test_worker_payout_split_50_50_when_configured(monkeypatch):
    # Re-import with a 50% AIPG share to prove the knob + exact sum.
    monkeypatch.setenv("GRID_WORKER_AIPG_SHARE_BPS", "5000")
    econ = importlib.reload(economics)
    try:
        p = econ.split_worker_payout(500_001)  # odd → dust check
        assert p["aipg_value"] == 250_000      # floor(500001 * 5000/10000)
        assert p["usdc"] == 250_001            # remainder keeps the sum exact
        assert p["usdc"] + p["aipg_value"] == 500_001
    finally:
        monkeypatch.delenv("GRID_WORKER_AIPG_SHARE_BPS", raising=False)
        importlib.reload(economics)  # restore module-level defaults for other tests


def test_aipg_bonus_off_by_default():
    assert economics.aipg_payment_bonus_micro(1_000_000) == 0
