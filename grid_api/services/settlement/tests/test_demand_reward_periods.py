# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from grid_api.config import GridSettings, WorkerRewardDemandPolicy
from grid_api.services.settlement import payouts as P
from . import test_payouts_lifecycle as lifecycle
from .test_payouts_lifecycle import START, END, PERIOD, WALLET

db = lifecycle.db
isolated_screening = lifecycle.isolated_screening


def policy(**changes):
    return WorkerRewardDemandPolicy(**dict(
        {"since": START, "until": END, "price_micro_per_aipg": 1000}, **changes))


def settings(monkeypatch, policies):
    monkeypatch.setattr(P, "get_settings", lambda: SimpleNamespace(
        worker_rewards_paid_only_since=START - timedelta(hours=1),
        worker_reward_demand_policies=policies))


@pytest.mark.parametrize("change", [
    {"since": START.replace(tzinfo=None)}, {"since": START + timedelta(minutes=1)},
    {"until": START}, {"until": START + timedelta(days=8)},
    {"price_micro_per_aipg": 0}, {"price_micro_per_aipg": True},
    {"worker_share_bps": 10000}, {"unexpected": True},
])
def test_reject_invalid_policy(change):
    with pytest.raises(ValidationError):
        policy(**change)


def test_settings_require_paid_cutoff_and_contiguous_retained_epochs():
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None, worker_reward_demand_policies=[policy()])
    for later in [policy(), policy(since=END + timedelta(hours=1), until=END + timedelta(hours=2))]:
        with pytest.raises(ValidationError):
            GridSettings(_env_file=None, worker_rewards_paid_only_since=START,
                         worker_reward_demand_policies=[policy(), later])
    result = GridSettings(_env_file=None, worker_rewards_paid_only_since=START,
                         worker_reward_demand_policies=[policy(), policy(since=END, until=END + timedelta(hours=1))])
    assert len(result.worker_reward_demand_policies) == 2


def test_epoch_boundary_expiry_and_history(monkeypatch):
    settings(monkeypatch, [policy()])
    old_start = START - timedelta(hours=1)
    old = P._period_contract(old_start, START, 100, old_start.strftime("hour-%Y-%m-%dT%H"))
    assert old["version"] == 1 and "demand_policy" not in old
    assert P._period_contract(START, END, 100, PERIOD)["version"] == 2
    with pytest.raises(ValueError, match="reviewed"):
        P._period_contract(END, END + timedelta(hours=1), 100, END.strftime("hour-%Y-%m-%dT%H"))


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed,paid", [(10000, "8.5"), (1000, "0.85")])
async def test_sender_freezes_cap_and_backing_no_reprice_after_late_demand(db, monkeypatch, consumed, paid):
    if db.dialect.name == "sqlite" and paid == "0.85":
        pytest.skip("SQLite NUMERIC stores 0.85 as a float; exact persisted-money proof requires PostgreSQL")
    settings(monkeypatch, [policy()])
    account = uuid.uuid4()
    async with await P.new_session() as session:
        await session.execute(P.accounts_t.insert().values(id=account, payout_wallet=WALLET))
        await session.commit()
    aggregate = AsyncMock(return_value=([
        {"account_id": str(account), "den": 100, "payout_address": WALLET},
    ], {str(account): consumed}))
    monkeypatch.setattr(P, "funded_work_by_account", aggregate)
    monkeypatch.setattr(P, "total_den_in_window", AsyncMock(return_value=100))
    monkeypatch.setattr(P, "BASE_RPC_URL", "")
    monkeypatch.setattr(P, "TREASURY_PK", "")
    monkeypatch.setattr(P, "_ctx", lambda: pytest.fail("test cannot sign"))
    preview = await P.preview_period(START, END, 100, period_id=PERIOD)
    assert preview["no_account_den"] is None
    P.total_den_in_window.assert_not_awaited()
    assert preview["payouts"][0]["aipg"] == Decimal(paid)
    assert preview["unallocated_aipg"] == float(100 - Decimal(paid))
    assert (await P.send_period(START, END, 100, PERIOD))["failed"] == 1
    row = await P._row(PERIOD, account)
    assert row["aipg_amount"] == Decimal(paid) and row["nonce"] is None
    aggregate.reset_mock()
    aggregate.side_effect = AssertionError("must not reaggregate")
    settings(monkeypatch, [policy(), policy(since=END, until=END + timedelta(hours=1), price_micro_per_aipg=2000)])
    await P.send_period(START, END, 100, PERIOD)
    replay = await P.preview_period(START, END, 100, period_id=PERIOD)
    assert replay["payouts"][0]["share"] == float(Decimal(paid) / 100)
    assert replay["demand_policy"] == preview["demand_policy"]
    aggregate.assert_not_awaited()
    async with await P.new_session() as session:
        plan = await session.scalar(sa.select(P.periods_t.c.plan).where(P.periods_t.c.period_id == PERIOD))
    assert plan["allocations"][0]["purchased_micro"] == consumed
    settings(monkeypatch, [policy(price_micro_per_aipg=500)])
    with pytest.raises(ValueError, match="conflicts"):
        await P.send_period(START, END, 100, PERIOD)


@pytest.mark.asyncio
async def test_downgrade_cannot_restore_full_budget(db, monkeypatch):
    settings(monkeypatch, [policy()])
    expected = P._period_contract(START, END, 100, PERIOD)
    await P.payout_periods.freeze(PERIOD, expected, AsyncMock(return_value=[]))
    settings(monkeypatch, [])
    next_id = END.strftime("hour-%Y-%m-%dT%H")
    legacy = P._period_contract(END, END + timedelta(hours=1), 100, next_id)
    build = AsyncMock(return_value=[])
    with pytest.raises(ValueError, match="downgrade"):
        await P.payout_periods.freeze(next_id, legacy, build)
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_downgrade_cannot_backfill_missed_hour_after_activation(db, monkeypatch):
    settings(monkeypatch, [policy(until=END + timedelta(hours=1))])
    later = END.strftime("hour-%Y-%m-%dT%H")
    expected = P._period_contract(END, END + timedelta(hours=1), 100, later)
    await P.payout_periods.freeze(later, expected, AsyncMock(return_value=[]))
    settings(monkeypatch, [])
    build = AsyncMock(return_value=[])
    with pytest.raises(ValueError, match="downgrade"):
        await P.payout_periods.freeze(PERIOD, P._period_contract(START, END, 100, PERIOD), build)
    build.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backing", [None, 0, 1])
async def test_freezer_independently_rejects_unbacked_allocation(db, monkeypatch, backing):
    settings(monkeypatch, [policy()])
    row = {"account_id": str(uuid.uuid4()), "den": 100, "aipg": 1,
           "payout_address": WALLET, "purchased_micro": backing}
    with pytest.raises(ValueError, match="purchased"):
        await P.payout_periods.freeze(PERIOD, P._period_contract(START, END, 100, PERIOD), AsyncMock(return_value=[row]))
    async with await P.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(P.periods_t)) == 0
