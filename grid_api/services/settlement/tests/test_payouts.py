# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure-math tests for custodial payout splitting (no DB/web3)."""

from grid_api.services.settlement.payouts import compute_payouts


def test_prorata_split_sums_to_budget():
    rows = [{"address": "0xA", "den": 30.0}, {"address": "0xB", "den": 10.0}]
    out = compute_payouts(rows, budget_aipg=100.0, min_aipg=0.0)
    by = {o["address"]: o for o in out}
    assert round(by["0xA"]["aipg"], 6) == 75.0   # 30/40
    assert round(by["0xB"]["aipg"], 6) == 25.0   # 10/40
    assert round(sum(o["aipg"] for o in out), 6) == 100.0
    assert out[0]["address"] == "0xA"            # sorted high→low


def test_dust_dropped_and_sorted():
    rows = [{"address": "0xBig", "den": 999.0}, {"address": "0xDust", "den": 0.001}]
    out = compute_payouts(rows, budget_aipg=100.0, min_aipg=0.01)
    assert [o["address"] for o in out] == ["0xBig"]   # dust below min_aipg dropped


def test_empty_and_zero_budget():
    assert compute_payouts([], 100.0) == []
    assert compute_payouts([{"address": "0xA", "den": 5.0}], 0.0) == []
    assert compute_payouts([{"address": "0xA", "den": 0.0}], 100.0) == []
