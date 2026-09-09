# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure-math tests for account-based custodial payout splitting (no DB/web3)."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from grid_api.services.settlement import payouts
from grid_api.services.settlement.payouts import _as_uuid, compute_account_payouts


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


def test_as_uuid_coerces_str_account_id():
    # Regression: aggregation returns account_id as a str; comparing that to the
    # sa.Uuid column crashed ("'str' has no attribute 'hex'") on sqlite/CI.
    u = uuid.uuid4()
    assert _as_uuid(str(u)) == u                 # str → UUID
    assert _as_uuid(u) is u                       # UUID passes through
    assert _as_uuid(None) is None                 # None passes through
    assert isinstance(_as_uuid(str(u)), uuid.UUID)
    assert _as_uuid("not-a-uuid") == "not-a-uuid" # garbage passes through (no crash)


@pytest.mark.parametrize("accounts", [1, 10, 1000])
def test_smollm_cap_is_network_wide_not_per_wallet(accounts):
    rows = [{"account_id": str(i), "den": 100.0 / accounts, "smollm_den": 100.0 / accounts,
             "payout_address": None if i % 2 else "0xA"} for i in range(accounts)]
    out = compute_account_payouts(rows, 208.33, min_aipg=0)
    paid = sum(Decimal(str(p["aipg"])) for p in out)
    assert paid <= Decimal("1.04165")
    assert paid >= Decimal("1.04164")


def test_clipped_share_is_not_redistributed_even_with_mixed_account():
    rows = [
        {"account_id": "mixed", "den": 90, "smollm_den": 80, "payout_address": "0xA"},
        {"account_id": "other", "den": 10, "smollm_den": 0, "payout_address": None},
    ]
    out = {r["account_id"]: r for r in compute_account_payouts(rows, 100, min_aipg=0)}
    assert out["mixed"]["aipg"] == 10.5
    assert out["other"]["aipg"] == 10
    assert sum(r["aipg"] for r in out.values()) == 20.5


def test_small_leg_below_cap_keeps_its_smaller_original_share():
    rows = [{"account_id": "small", "den": 0.1, "smollm_den": 0.1},
            {"account_id": "other", "den": 99.9, "smollm_den": 0}]
    out = {r["account_id"]: r for r in compute_account_payouts(rows, 100, min_aipg=0)}
    assert out["small"]["aipg"] == 0.1


@pytest.mark.parametrize("small", [-1, 101, float("nan"), float("inf")])
def test_malformed_capped_weights_reject(small):
    with pytest.raises(ValueError):
        compute_account_payouts([{"account_id": "bad", "den": 100, "smollm_den": small}], 100)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["den", "budget", "min_aipg"])
@pytest.mark.parametrize("capped", [False, True])
def test_all_payout_inputs_must_be_finite_and_nonnegative(bad, field, capped):
    rows = [{"account_id": "A", "den": 2, "payout_address": None}]
    if capped:
        rows[0]["smollm_den"] = 0
    if field == "den":
        rows.append({"account_id": "B", "den": bad, "payout_address": "0xB"})
    with pytest.raises(ValueError):
        compute_account_payouts(
            rows, bad if field == "budget" else 100,
            min_aipg=bad if field == "min_aipg" else 0,
        )


def test_overflowing_total_den_rejects_instead_of_zero_allocations():
    rows = [{"account_id": str(i), "den": 1e308} for i in range(2)]
    with pytest.raises(ValueError):
        compute_account_payouts(rows, 100, min_aipg=0)


@pytest.mark.parametrize("rows,budget", [([], float("nan")), ([{"den": -1}], 0),
                                         ([{"den": 0, "smollm_den": 1}], 100)])
def test_empty_or_zero_budget_does_not_hide_invalid_inputs(rows, budget):
    with pytest.raises(ValueError):
        compute_account_payouts(rows, budget)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_budget,bad_weight", [(float("inf"), 1), (100, -1),
                                                  (100, float("nan"))])
async def test_invalid_allocation_never_enters_sender_or_writes_accrual(
    monkeypatch, bad_budget, bad_weight,
):
    rows = [{"account_id": "walletless", "den": 2, "payout_address": None},
            {"account_id": "payable", "den": bad_weight, "payout_address": "0xA"}]
    monkeypatch.setattr(payouts, "aggregate_den_by_account", AsyncMock(return_value=rows))
    monkeypatch.setattr(payouts, "BASE_RPC_URL", "https://never.invalid")
    monkeypatch.setattr(payouts, "TREASURY_PK", "test-only-invalid-key")
    sender = Mock(side_effect=AssertionError("Must not initialize the sender"))
    writes = AsyncMock(side_effect=AssertionError("Must not write an allocation"))
    lookups = AsyncMock(side_effect=AssertionError("Must reject before payout lookup"))
    monkeypatch.setattr(payouts, "_ctx", sender)
    monkeypatch.setattr(payouts, "_write", writes)
    monkeypatch.setattr(payouts, "_row", lookups)

    with pytest.raises(ValueError):
        await payouts.send_period(None, None, bad_budget, "test-period")
    sender.assert_not_called()
    writes.assert_not_called()
    lookups.assert_not_called()
