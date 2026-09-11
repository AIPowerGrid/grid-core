# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from decimal import Decimal
import uuid

import pytest

from grid_api.services.settlement.demand_rewards import bound_allocations


def allocate(rows, backing, **kwargs):
    return bound_allocations(rows, backing, price_micro_per_aipg=1000,
                             worker_share_bps=8500, **kwargs)


def test_tiny_self_demand_cannot_take_hourly_budget():
    account = str(uuid.uuid4())
    rows = [{"account_id": account, "aipg": 208.33}]
    assert allocate(rows, {account: 1})[0]["aipg"] == Decimal("0.00085000")
    assert rows[0]["aipg"] == 208.33


def test_no_backing_no_reward_even_with_huge_den_allocation():
    account = str(uuid.uuid4())
    assert allocate([{"account_id": account, "aipg": 208.33}], {}) == []
    assert allocate([{"account_id": account, "aipg": 208.33}], {account: 0}) == []


def test_attacker_cannot_borrow_honest_workers_backing_or_clipped_budget():
    honest, attacker = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [{"account_id": honest, "aipg": 8.33}, {"account_id": attacker, "aipg": 200}]
    out = allocate(rows, {honest: 1000000, attacker: 1})
    assert [r["aipg"] for r in out] == [Decimal("8.33000000"), Decimal("0.00085000")]


def test_many_identities_do_not_multiply_backing_and_rounding_is_down():
    keys = [str(uuid.uuid4()) for _ in range(100)]
    rows = [{"account_id": key, "aipg": 2} for key in keys]
    out = bound_allocations(rows, dict.fromkeys(keys, 1), price_micro_per_aipg=3333, worker_share_bps=8500)
    total = sum((r["aipg"] for r in out), Decimal(0))
    assert total <= Decimal(100 * 8500) / (10000 * 3333)
    assert total < Decimal("0.026")


def test_demand_branch_natural_allocations_never_round_above_global_ceiling():
    from grid_api.services.settlement.payouts import compute_account_payouts

    rows = [{"account_id": str(uuid.uuid4()), "den": 1} for _ in range(6)]
    allocation = compute_account_payouts(rows, 1, conservative_rounding=True)
    assert sum(Decimal(str(row["aipg"])) for row in allocation) == Decimal("0.99999996")


@pytest.mark.parametrize("value", [-1, True, 0.1, "10", None, 10**29])
def test_malformed_backing_rejects_whole_batch(value):
    account = str(uuid.uuid4())
    with pytest.raises(ValueError):
        allocate([{ "account_id": account, "aipg": 1}], {account: value})


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", "-Infinity", 10**29])
def test_invalid_allocation_rejected(value):
    account = str(uuid.uuid4())
    with pytest.raises(ValueError):
        allocate([{ "account_id": account, "aipg": value}], {account: 100})


def test_duplicate_accounts_including_uuid_aliases_are_rejected():
    account = uuid.uuid4()
    rows = [{"account_id": str(account), "aipg": 1}, {"account_id": account.hex, "aipg": 1}]
    with pytest.raises(ValueError, match="duplicate"):
        allocate(rows, {str(account): 1})
    with pytest.raises(ValueError, match="duplicate"):
        allocate([], {str(account): 1, account.hex: 1})


@pytest.mark.parametrize("price,share", [(0, 8500), (-1, 8500), (True, 8500), (1, 10000), (1, 0), (1, 8500.0)])
def test_invalid_policy_rejected_even_for_empty_input(price, share):
    with pytest.raises(ValueError):
        bound_allocations([], {}, price_micro_per_aipg=price, worker_share_bps=share)
