# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prospective payout plans, immutable intents and legacy exclusion."""

import asyncio
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from grid_api.services.settlement import payouts as P

from . import test_payouts_lifecycle as lifecycle
from .test_payouts_lifecycle import (
    END,
    PERIOD,
    START,
    WALLET,
    _ctx,
    _FakeEth,
    _freeze,
    _hash_for,
    _transfer_receipt,
)

db = lifecycle.db
isolated_screening = lifecycle.isolated_screening

OTHER = "0x1111111111111111111111111111111111111111"


@pytest.mark.parametrize("change", ["naive", "partial", "overlap", "open", "historical", "alias", "over_budget", "no_cutoff"])
def test_invalid_periods_reject_before_database(monkeypatch, change):
    start, end, budget, pid = START, END, 100, PERIOD
    if change == "naive":
        start = start.replace(tzinfo=None)
    elif change == "partial":
        end -= dt.timedelta(minutes=1)
    elif change == "overlap":
        start += dt.timedelta(minutes=30)
        end += dt.timedelta(minutes=30)
    elif change == "open":
        end += dt.timedelta(hours=2)
        start += dt.timedelta(hours=2)
        pid = start.strftime("hour-%Y-%m-%dT%H")
    elif change == "historical":
        start -= dt.timedelta(hours=1)
        end -= dt.timedelta(hours=1)
        pid = start.strftime("hour-%Y-%m-%dT%H")
    elif change == "alias":
        pid = "same-hour-different-name"
    elif change == "over_budget":
        budget = 209
    else:
        monkeypatch.setattr(P, "get_settings", lambda: type("Settings", (), {"worker_rewards_paid_only_since": None})())
    with pytest.raises(ValueError):
        P._period_contract(start, end, budget, pid)


@pytest.mark.asyncio
async def test_partial_send_late_jobs_and_wallet_change_never_reprice(db, monkeypatch):
    first, second, late = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    aggregate = AsyncMock(
        return_value=[
            {"account_id": str(first), "den": 1, "payout_address": WALLET},
            {"account_id": str(second), "den": 1, "payout_address": OTHER},
        ],
    )
    monkeypatch.setattr(P, "aggregate_den_by_account", aggregate)
    eth = _FakeEth()
    # Keep both in-flight, then record one confirmed payment before the retry.
    monkeypatch.setattr(P, "_ctx", lambda: _ctx(eth))
    monkeypatch.setattr(P, "BASE_RPC_URL", "test")
    monkeypatch.setattr(P, "TREASURY_PK", "test")
    assert (await P.send_period(START, END, 100, PERIOD))["pending"] == 2
    first_row = await P._row(PERIOD, first)
    await P._write(PERIOD, first, address=WALLET, den=1, aipg=50, status="sent", nonce=first_row["nonce"], set_tx=False, paid=True)
    aggregate.return_value = [
        {"account_id": str(first), "den": 1, "payout_address": OTHER},
        {"account_id": str(second), "den": 1, "payout_address": WALLET},
        {"account_id": str(late), "den": 2, "payout_address": WALLET},
    ]
    result = await P.send_period(START, END, 100.0, PERIOD)
    assert result["skipped"] == 1 and result["pending"] == 1
    aggregate.assert_awaited_once()
    rows = await P._planned_rows(PERIOD)
    assert sum(row["aipg_amount"] for row in rows) == Decimal(100)
    assert (await P._row(PERIOD, second))["address"] == OTHER
    assert await P._row(PERIOD, late) is None
    with pytest.raises(ValueError, match="conflicts"):
        await P.send_period(START, END, 101, PERIOD)
    assert len(eth.broadcasts) == 3


@pytest.mark.asyncio
async def test_empty_period_stays_empty_and_retry_does_not_aggregate(db):
    build = AsyncMock(return_value=[])
    contract = P._period_contract(START, END, 100, PERIOD)
    await P.payout_periods.freeze(PERIOD, contract, build)
    build.side_effect = AssertionError("must not reaggregate")
    await P.payout_periods.freeze(PERIOD, contract, build)
    assert await P._planned_rows(PERIOD) == []
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_and_allocations_rollback_together(db):
    acct = uuid.uuid4()

    def fail_insert(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO grid_payouts "):
            raise RuntimeError("interrupted allocation insert")

    sa.event.listen(db.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            await _freeze(acct)
    finally:
        sa.event.remove(db.sync_engine, "before_cursor_execute", fail_insert)
    async with await P.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(P.periods_t)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(P.payouts_t)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("aipg", 2), ("den", 2), ("address", OTHER), ("nonce", 2)])
async def test_bound_intent_cannot_change(db, field, value):
    acct = uuid.uuid4()
    await _freeze(acct)
    args = dict(address=WALLET, den=1, aipg=1, status="pending", nonce=1, tx_hash=str(_hash_for(1)))
    await P._write(PERIOD, acct, **args)
    args[field] = value
    with pytest.raises(ValueError):
        await P._write(PERIOD, acct, **args)
    row = await P._row(PERIOD, acct)
    assert row["aipg_amount"] == 1 and row["address"] == WALLET and row["nonce"] == 1


@pytest.mark.asyncio
async def test_legacy_rows_are_not_adopted_or_retried(db, monkeypatch):
    acct = uuid.uuid4()
    async with await P.new_session() as session:
        await session.execute(P.accounts_t.insert().values(id=acct, payout_wallet=WALLET))
        await session.commit()
    await P._write("old-accrual", acct, address=None, den=1, aipg=1, status="accrued")
    await P._write(PERIOD, acct, address=WALLET, den=1, aipg=1, status="pending", nonce=4, tx_hash=str(_hash_for(4)))
    monkeypatch.setattr(P, "_ctx", lambda: pytest.fail("legacy records must not reach a signer"))
    assert (await P.reconcile_and_retry())["settled"] == 0
    assert (await P.pay_accrued())["paid"] == 0
    with pytest.raises(ValueError, match="legacy"):
        await P.send_period(START, END, 1, PERIOD)
    assert (await P._row(PERIOD, acct))["nonce"] == 4
    assert (await P._row("old-accrual", acct))["status"] == "accrued"


@pytest.mark.asyncio
async def test_walletless_amount_survives_later_wallet_binding(db, monkeypatch):
    acct = uuid.uuid4()
    await _freeze(acct, address=None)
    async with await P.new_session() as session:
        await session.execute(P.accounts_t.insert().values(id=acct, payout_wallet=WALLET))
        await session.commit()
    eth = _FakeEth()
    eth.receipts[str(_hash_for(0))] = _transfer_receipt(WALLET, 1)
    monkeypatch.setattr(P, "_ctx", lambda: _ctx(eth))
    assert (await P.pay_accrued())["paid"] == 1
    assert (await P._row(PERIOD, acct))["address"] == WALLET
    assert (await P.pay_accrued())["paid"] == 0
    assert len(eth.broadcasts) == 1


@pytest.mark.asyncio
async def test_plan_commitment_and_row_tampering_fail_closed(db):
    acct = uuid.uuid4()
    await _freeze(acct)
    async with await P.new_session() as session:
        await session.execute(P.payouts_t.update().values(aipg_amount=2))
        await session.commit()
    with pytest.raises(ValueError, match="allocation"):
        await P._planned_rows(PERIOD)


@pytest.mark.asyncio
async def test_postgres_concurrent_freeze_has_one_snapshot(db):
    if db.dialect.name != "postgresql":
        pytest.skip("real PostgreSQL plan serialization")
    acct = uuid.uuid4()
    build = AsyncMock(return_value=[{"account_id": str(acct), "den": 1, "aipg": 1, "payout_address": WALLET}])
    contract = P._period_contract(START, END, 1, PERIOD)
    results = await asyncio.gather(*(P.payout_periods.freeze(PERIOD, contract, build) for _ in range(10)))
    assert all(result == results[0] for result in results)
    build.assert_awaited_once()
    assert len(await P._planned_rows(PERIOD)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["send", "accrued", "retry"])
async def test_imported_senders_share_lock_for_entire_send(db, monkeypatch, competitor):
    if db.dialect.name != "postgresql":
        pytest.skip("real PostgreSQL sender serialization")
    started, release = asyncio.Event(), asyncio.Event()
    acct = uuid.uuid4()
    monkeypatch.setattr(
        P,
        "aggregate_den_by_account",
        AsyncMock(
            return_value=[
                {"account_id": str(acct), "den": 1, "payout_address": WALLET},
            ],
        ),
    )
    monkeypatch.setattr(P, "_ctx", lambda: _ctx(_FakeEth()))
    monkeypatch.setattr(P, "BASE_RPC_URL", "test")
    monkeypatch.setattr(P, "TREASURY_PK", "test")

    async def paused_send(*args, **kwargs):
        started.set()
        await release.wait()
        return "pending"

    monkeypatch.setattr(P, "_settle_one", paused_send)
    task = asyncio.create_task(P.send_period(START, END, 1, PERIOD))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="treasury lock"):
            if competitor == "send":
                await P.send_period(START, END, 1, PERIOD)
            elif competitor == "accrued":
                await P.pay_accrued()
            else:
                await P.reconcile_and_retry()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)
    # The transaction-scoped lock was actually released, not left in the pool.
    assert (await P.send_period(START, END, 1, PERIOD))["pending"] == 1


@pytest.mark.asyncio
async def test_preview_reuses_frozen_allocations(db, monkeypatch):
    await _freeze(uuid.uuid4())
    aggregate = AsyncMock(side_effect=AssertionError("preview must not reprice"))
    monkeypatch.setattr(P, "aggregate_den_by_account", aggregate)
    preview = await P.preview_period(START, END, 1, period_id=PERIOD)
    assert preview["frozen"] and preview["payable_now_aipg"] == 1
    aggregate.assert_not_awaited()


def test_exact_token_units():
    assert P._token_units(Decimal("208.33000001"), 18) == 208330000010000000000
    with pytest.raises(ValueError):
        P._token_units(Decimal("0.00000001"), 6)


@pytest.mark.asyncio
async def test_migration_refuses_to_discard_nonempty_plan(db):
    await _freeze(uuid.uuid4())
    path = Path(__file__).resolve().parents[4] / "alembic/versions/0041_frozen_worker_payout_periods.py"
    spec = importlib.util.spec_from_file_location("payout_migration_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with db.begin() as connection:

        def downgrade(sync_connection):
            migration.op = Operations(MigrationContext.configure(sync_connection))
            with pytest.raises(RuntimeError, match="refuse to discard"):
                migration.downgrade()

        await connection.run_sync(downgrade)
    assert len(await P._planned_rows(PERIOD)) == 1


def test_hourly_wrapper_uses_one_clock_read_and_immutable_runtime(tmp_path):
    # Run a copied wrapper with a recorder instead of Python: no Grid imports,
    # signing, DB or network. A controlled date executable checks every read.
    root = tmp_path / "release"
    scripts = root / "scripts"
    bin_path = root / ".venv/bin"
    scripts.mkdir(parents=True)
    bin_path.mkdir(parents=True)
    source = Path(__file__).resolve().parents[4] / "scripts/payout_hourly.sh"
    wrapper = scripts / "payout_hourly.sh"
    wrapper.write_text(source.read_text())
    recorder = bin_path / "python"
    recorder.write_text(
        f"#!{sys.executable}\n"
        + "import json, os, sys\nwith open(os.environ['ARGS_FILE'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
    )
    recorder.chmod(0o700)
    clock = bin_path / "date"
    clock.write_text(f"#!{sys.executable}\n" + """import datetime as dt, os, sys
args = sys.argv[1:]
if args[-1] == '+%s':
    with open(os.environ['CLOCK_FILE'], 'a') as f: f.write('read\\n')
    print(os.environ['TEST_EPOCH'])
else:
    epoch = args[args.index('-d') + 1]
    assert epoch.startswith('@'), 'must derive all fields from captured epoch'
    print(dt.datetime.fromtimestamp(int(epoch[1:]), dt.UTC).strftime(args[-1][1:]))
""")
    clock.chmod(0o700)
    args_file, clock_file = tmp_path / "args.jsonl", tmp_path / "clock.txt"
    env = dict(
        os.environ,
        PATH=f"{bin_path}:{os.environ['PATH']}",
        ARGS_FILE=str(args_file),
        CLOCK_FILE=str(clock_file),
        TEST_EPOCH=str(int((END + dt.timedelta(seconds=1)).timestamp())),
        PAYOUT_HOURLY_BUDGET="208.33",
    )
    subprocess.run(["bash", str(wrapper)], env=env, check=True, capture_output=True, timeout=10)
    commands = [json.loads(line) for line in args_file.read_text().splitlines()]
    assert clock_file.read_text() == "read\n"
    assert commands == [
        [
            "-m",
            "grid_api.services.settlement.payouts",
            "--since",
            START.isoformat(),
            "--until",
            END.isoformat(),
            "--period-id",
            PERIOD,
            "--budget",
            "208.33",
            "--send",
        ],
        ["-m", "grid_api.services.settlement.payouts", "--retry-failed"],
    ]
