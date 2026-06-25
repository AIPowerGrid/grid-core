# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure-math tests for account-based custodial payout splitting (no DB/web3)."""

from grid_api.services.settlement.payouts import compute_account_payouts


def test_prorata_split_by_den_sums_to_budget():
    rows = [
        {"account_id": "A", "den": 30.0, "payout_address": "0xA"},
        {"account_id": "B", "den": 10.0, "payout_address": "0xB"},
    ]
    out = {o["account_id"]: o for o in compute_account_payouts(rows, 100.0, min_aipg=0.0)}
    assert round(out["A"]["aipg"], 6) == 75.0   # 30/40
    assert round(out["B"]["aipg"], 6) == 25.0   # 10/40
    assert all(o["payable"] for o in out.values())


def test_no_wallet_accrues_but_keeps_its_share():
    # The wallet-less account still gets its true den-share — just marked not payable.
    rows = [
        {"account_id": "A", "den": 50.0, "payout_address": "0xA"},
        {"account_id": "B", "den": 50.0, "payout_address": None},
    ]
    out = {o["account_id"]: o for o in compute_account_payouts(rows, 100.0, min_aipg=0.0)}
    assert out["A"]["aipg"] == 50.0 and out["A"]["payable"] is True
    assert out["B"]["aipg"] == 50.0 and out["B"]["payable"] is False  # accrues, not redistributed


def test_dust_dropped_and_sorted_desc():
    rows = [{"account_id": "Big", "den": 999.0, "payout_address": "0x1"},
            {"account_id": "Dust", "den": 0.001, "payout_address": "0x2"}]
    out = compute_account_payouts(rows, 100.0, min_aipg=0.01)
    assert [o["account_id"] for o in out] == ["Big"]


def test_empty_and_zero_budget():
    assert compute_account_payouts([], 100.0) == []
    assert compute_account_payouts([{"account_id": "A", "den": 5.0, "payout_address": "0x"}], 0.0) == []
