# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import validator_audits
from grid_api.v2.schema import ledger as ledger_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_audit_reservations as reservations_t

WALLET = "0x" + "a" * 40
WALLET_2 = "0x" + "e" * 40


def _settings(**overrides):
    values = {
        "validator_paid_audit_enabled": True,
        "validator_paid_audit_wallets": WALLET,
        "validator_paid_audit_daily_den": 25.0,
        "validator_paid_audit_hourly_den": 20.0,
        "validator_paid_audit_per_validator_daily_den": 20.0,
        "validator_paid_audit_per_worker_daily_den": 20.0,
        "validator_paid_audit_max_den_per_job": 10.0,
        "validator_paid_audit_stale_seconds": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(validator_audits, "get_settings", lambda: _settings())
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


def _reservation_args(job_id=None, **overrides):
    values = {
        "job_id": str(job_id or uuid.uuid4()),
        "assignment_id": f"asg_{uuid.uuid4().hex}",
        "probe_group_id": f"prg_{uuid.uuid4().hex}",
        "worker_id": str(uuid.uuid4()),
        "validator_wallet": WALLET,
    }
    values.update(overrides)
    return values


def _ledger_values(args, den):
    return {
        "job_id": args["job_id"],
        "worker_id": args["worker_id"],
        "wallet": "0x" + "b" * 40,
        "model": "model-a",
        "job_type": "text",
        "den": den,
        "output_units": 7,
        "prompt_hash": "c" * 64,
        "result_hash": "d" * 64,
    }


def _terminal(args, text="a useful synthetic answer"):
    return {
        "worker_id": args["worker_id"],
        "grid_meta": {},
        "full_text": text,
        "full_reasoning": "",
        "tool_calls": [],
        "usage": {"completion_tokens": 4},
        "finish_reason": "stop",
    }


def test_paid_mode_fails_closed_without_complete_policy(monkeypatch):
    monkeypatch.setattr(
        validator_audits,
        "get_settings",
        lambda: _settings(validator_paid_audit_wallets="", validator_paid_audit_daily_den=0),
    )
    current = validator_audits.public_policy()
    assert current["requested"] is True
    assert current["enabled"] is False
    assert current["reasons"]
    with pytest.raises(validator_audits.AuditBudgetError):
        validator_audits.assignment_compensation(WALLET)


def test_paid_mode_rejects_unreviewed_validator_wallet(monkeypatch):
    monkeypatch.setattr(validator_audits, "get_settings", lambda: _settings())
    with pytest.raises(validator_audits.AuditBudgetError, match="not approved"):
        validator_audits.assignment_compensation("0x" + "f" * 40)


@pytest.mark.asyncio
async def test_reserve_and_atomic_settle_move_only_actual_den(db):
    args = _reservation_args()
    assert await validator_audits.reserve(**args) == "held"
    assert await validator_audits.reserve(**args) == "held"
    assert await validator_audits.snapshot() == {
        "budget_day": datetime.now(UTC).date().isoformat(),
        "limit_den": 25.0,
        "held_den": 10.0,
        "spent_den": 0.0,
        "remaining_den": 15.0,
    }

    status, paid = await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=_ledger_values(args, 3.25),
        terminal_result=_terminal(args),
    )
    assert (status, paid) == ("settled", 3.25)
    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(ledger_t.c.den, ledger_t.c.job_type).where(
                    ledger_t.c.job_id == uuid.UUID(args["job_id"]),
                ),
            )
        ).one()
    assert row == (3.25, "text")
    replay = await validator_audits.settled_result(args["job_id"])
    assert replay == (_terminal(args), 3.25)
    assert await validator_audits.snapshot() == {
        "budget_day": datetime.now(UTC).date().isoformat(),
        "limit_den": 25.0,
        "held_den": 0.0,
        "spent_den": 3.25,
        "remaining_den": 21.75,
    }


@pytest.mark.asyncio
async def test_per_job_and_daily_caps_are_enforced_before_dispatch(db):
    first = _reservation_args()
    second = _reservation_args()
    third = _reservation_args()
    await validator_audits.reserve(**first)
    await validator_audits.reserve(**second)
    with pytest.raises(validator_audits.AuditBudgetError, match="exhausted"):
        await validator_audits.reserve(**third)
    assert (await validator_audits.snapshot())["held_den"] == 20.0

    status, paid = await validator_audits.record_and_settle(
        job_id=first["job_id"],
        ledger_values=_ledger_values(first, 999),
        terminal_result=_terminal(first),
    )
    assert (status, paid) == ("settled", 10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "first_overrides", "second_overrides", "message"),
    (
        (
            {"validator_paid_audit_hourly_den": 10.0},
            {},
            {"worker_id": str(uuid.uuid4()), "validator_wallet": WALLET_2},
            "hourly",
        ),
        (
            {"validator_paid_audit_per_validator_daily_den": 10.0},
            {},
            {"worker_id": str(uuid.uuid4())},
            "per-validator",
        ),
        (
            {"validator_paid_audit_per_worker_daily_den": 10.0},
            {},
            {"validator_wallet": WALLET_2},
            "per-worker",
        ),
    ),
)
async def test_scoped_caps_are_enforced_before_dispatch(
    db,
    monkeypatch,
    settings,
    first_overrides,
    second_overrides,
    message,
):
    limits = {
        "validator_paid_audit_wallets": f"{WALLET},{WALLET_2}",
        "validator_paid_audit_daily_den": 100.0,
        "validator_paid_audit_hourly_den": 100.0,
        "validator_paid_audit_per_validator_daily_den": 100.0,
        "validator_paid_audit_per_worker_daily_den": 100.0,
        **settings,
    }
    monkeypatch.setattr(
        validator_audits,
        "get_settings",
        lambda: _settings(**limits),
    )
    first = _reservation_args(**first_overrides)
    second = _reservation_args(**second_overrides)
    if message == "per-worker":
        second["worker_id"] = first["worker_id"]

    await validator_audits.reserve(**first)
    with pytest.raises(validator_audits.AuditBudgetError, match=message):
        await validator_audits.reserve(**second)


@pytest.mark.asyncio
async def test_idempotent_reserve_rejects_job_id_rebinding(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    rebound = {**args, "assignment_id": f"asg_{uuid.uuid4().hex}"}
    with pytest.raises(validator_audits.AuditBudgetError, match="different work"):
        await validator_audits.reserve(**rebound)


@pytest.mark.asyncio
async def test_terminal_result_is_json_safe_bounded_and_atomic(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)

    status, paid = await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=_ledger_values(args, 4),
        terminal_result={"bad": object()},
    )
    assert (status, paid) == ("error", 0.0)
    assert await validator_audits.settled_result(args["job_id"]) is None
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger_t)) == 0

    status, paid = await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=_ledger_values(args, 4),
        terminal_result={"full_text": "x" * (validator_audits._TERMINAL_RESULT_MAX_BYTES + 1)},
    )
    assert (status, paid) == ("error", 0.0)
    assert await validator_audits.settled_result(args["job_id"]) is None


@pytest.mark.asyncio
async def test_release_then_late_success_cannot_mint_payout(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    assert await validator_audits.release(args["job_id"]) == "released"
    assert await validator_audits.release(args["job_id"]) == "released"
    with pytest.raises(validator_audits.AuditBudgetError, match="already terminal"):
        await validator_audits.reserve(**args)
    status, paid = await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=_ledger_values(args, 4),
        terminal_result=_terminal(args),
    )
    assert (status, paid) == ("stale_no_payout", 0.0)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger_t)) == 0
    assert (await validator_audits.snapshot())["remaining_den"] == 25.0


@pytest.mark.asyncio
async def test_settlement_rejects_job_substitution_without_moving_money(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    values = _ledger_values(args, 4)
    values["job_id"] = str(uuid.uuid4())

    assert await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=values,
        terminal_result=_terminal(args),
    ) == ("error", 0.0)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger_t)) == 0
        status = await session.scalar(
            sa.select(reservations_t.c.status).where(
                reservations_t.c.job_id == uuid.UUID(args["job_id"]),
            ),
        )
    assert status == "held"
    assert (await validator_audits.snapshot())["held_den"] == 10.0


@pytest.mark.asyncio
async def test_settlement_rejects_worker_substitution_without_moving_money(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    values = _ledger_values(args, 4)
    values["worker_id"] = str(uuid.uuid4())

    assert await validator_audits.record_and_settle(
        job_id=args["job_id"],
        ledger_values=values,
        terminal_result=_terminal(args),
    ) == ("worker_mismatch", 0.0)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger_t)) == 0
        status = await session.scalar(
            sa.select(reservations_t.c.status).where(
                reservations_t.c.job_id == uuid.UUID(args["job_id"]),
            ),
        )
    assert status == "held"
    assert (await validator_audits.snapshot())["held_den"] == 10.0


@pytest.mark.asyncio
async def test_stale_sweeper_releases_crash_orphan(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    async with await database.new_session() as session:
        await session.execute(
            sa.update(reservations_t)
            .where(reservations_t.c.job_id == uuid.UUID(args["job_id"]))
            .values(created=datetime.now(UTC) - timedelta(hours=2)),
        )
        await session.commit()
    assert await validator_audits.sweep_stale(older_than_seconds=300) == 1
    assert (await validator_audits.snapshot())["held_den"] == 0.0
    health = await validator_audits.reservation_health()
    assert health["held"] == 0
    assert health["released"] == 1
    assert health["stale_held"] == 0
    assert health["terminal_invariant_breaches"] == 0


@pytest.mark.asyncio
async def test_reservation_constraints_reject_invalid_terminal_state(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    async with await database.new_session() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.update(reservations_t)
                .where(reservations_t.c.job_id == uuid.UUID(args["job_id"]))
                .values(status="settled", settled_den=None),
            )
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_reservation_constraints_reject_over_cap_settlement(db):
    args = _reservation_args()
    await validator_audits.reserve(**args)
    async with await database.new_session() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.update(reservations_t)
                .where(reservations_t.c.job_id == uuid.UUID(args["job_id"]))
                .values(status="settled", settled_den=11),
            )
            await session.commit()
        await session.rollback()
