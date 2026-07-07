# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pass-through payout distribution — pro-rata by den across per-asset pots.

Pure math (no DB): the intake ledger is exercised by an integration run; here we
prove the split is proportional, conserves each pot, and handles the edges."""

from grid_api.services import economics
from grid_api.services.settlement import revenue


ROWS = [
    {"account_id": "a", "den": 50.0, "payout_address": "0xaaa"},
    {"account_id": "b", "den": 30.0, "payout_address": "0xbbb"},
    {"account_id": "c", "den": 20.0, "payout_address": None},  # no wallet → accrues
]
POTS = {"USDC": 100.0, "ETH": 1.0, "AIPG": 1000.0}


def test_split_is_proportional_to_den():
    pay = revenue.compute_multiasset_payouts(ROWS, POTS)
    by = {p["account_id"]: p for p in pay}
    # a did 50% of den → 50 USDC, 0.5 ETH, 500 AIPG
    assert by["a"]["amounts"]["USDC"] == 50.0
    assert by["a"]["amounts"]["ETH"] == 0.5
    assert by["a"]["amounts"]["AIPG"] == 500.0
    assert by["b"]["amounts"]["USDC"] == 30.0
    assert by["c"]["amounts"]["AIPG"] == 200.0


def test_each_pot_is_conserved():
    pay = revenue.compute_multiasset_payouts(ROWS, POTS)
    for asset, pot in POTS.items():
        distributed = sum(p["amounts"].get(asset, 0.0) for p in pay)
        assert abs(distributed - pot) < 1e-9  # every unit of each pot is paid out


def test_no_wallet_still_gets_amounts_but_not_payable():
    pay = revenue.compute_multiasset_payouts(ROWS, POTS)
    c = next(p for p in pay if p["account_id"] == "c")
    assert c["payable"] is False
    assert c["amounts"]["USDC"] == 20.0  # accrues; still attributed


def test_worker_pots_applies_the_protocol_slice(monkeypatch):
    # 12% protocol + 3% sentinel → 85% worker share, per asset.
    monkeypatch.setattr(economics, "PROTOCOL_FEE_BPS", 1200)
    monkeypatch.setattr(economics, "SENTINEL_FEE_BPS", 300)
    wp = revenue.worker_pots({"USDC": 100.0, "ETH": 1.0})
    assert abs(wp["USDC"] - 85.0) < 1e-9
    assert abs(wp["ETH"] - 0.85) < 1e-9


def test_empty_and_degenerate():
    assert revenue.compute_multiasset_payouts([], POTS) == []
    assert revenue.compute_multiasset_payouts(ROWS, {}) == []
    zero_den = [{"account_id": "a", "den": 0.0, "payout_address": "0xaaa"}]
    assert revenue.compute_multiasset_payouts(zero_den, POTS) == []
