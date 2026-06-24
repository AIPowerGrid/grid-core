# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable per-job reservation lifecycle (worker-WS is the sole settler).

Proves the exactly-once settlement the auditor asked for: a 'held' row is opened
at reserve time and the terminal handler flips it held→settled, reconciling the
hold against actual grid-counted usage — refunding the unused remainder, fully
releasing a failed job, and being a strict no-op on any duplicate terminal (so a
disconnected client can never strand or double-settle a reservation).

Same in-memory-SQLite harness as test_credits_billing.py.
"""

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, pricing
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import reservations as reservations_t

PRICED = "gpt-oss-120b"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


async def _reserve(aid, job_id, prompt=1000, mx=1000):
    """Reserve at max and open the durable row atomically, as the request path does."""
    auth = await credits.authorize_request(
        {"account_id": aid}, PRICED, prompt, mx, job_id,
        record_reservation=True,
    )
    assert auth["ok"]
    return auth["reserved"]


async def _reservation_status(job_id):
    async with await database.new_session() as s:
        row = (await s.execute(
            sa.select(reservations_t.c.status).where(reservations_t.c.job_id == str(job_id))
        )).first()
        return row[0] if row else None


@pytest.mark.asyncio
async def test_authorize_records_reservation_atomically_and_idempotently(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    auth = await credits.authorize_request(
        {"account_id": aid}, PRICED, 1000, 1000, "job0",
        record_reservation=True,
    )
    assert auth["ok"] and auth["reserved"] > 0
    assert await _reservation_status("job0") == "held"
    assert await credits.get_balance(aid) == 10_000_000 - auth["reserved"]

    dup = await credits.authorize_request(
        {"account_id": aid}, PRICED, 1000, 1000, "job0",
        record_reservation=True,
    )
    assert dup["ok"] and dup["status"] == "already"
    assert await credits.get_balance(aid) == 10_000_000 - auth["reserved"]


@pytest.mark.asyncio
async def test_authorize_rolls_back_debit_if_reservation_write_fails(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("reservation unavailable")

    monkeypatch.setattr(credits, "_insert_reservation_in_session", boom)
    auth = await credits.authorize_request(
        {"account_id": aid}, PRICED, 1000, 1000, "job-reserve-fail",
        record_reservation=True,
    )
    assert auth["ok"] is False
    assert auth["status"] == "reservation_failed"
    assert await credits.get_balance(aid) == 10_000_000
    assert await _reservation_status("job-reserve-fail") is None


@pytest.mark.asyncio
async def test_settle_refunds_unused_remainder(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    reserved = await _reserve(aid, "jobA")
    assert await credits.get_balance(aid) == 10_000_000 - reserved

    # Terminal: actual completion 100 tokens (reserved for 1000).
    await credits.settle_job("jobA", 100)
    actual = pricing.quote_text(PRICED, 1000, 100)
    assert await credits.get_balance(aid) == 10_000_000 - actual

    # Duplicate terminal is a strict no-op (exactly-once).
    await credits.settle_job("jobA", 100)
    await credits.settle_job("jobA", 999)
    assert await credits.get_balance(aid) == 10_000_000 - actual


@pytest.mark.asyncio
async def test_release_full_refund(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    reserved = await _reserve(aid, "jobB")
    assert reserved > 0 and await credits.get_balance(aid) == 10_000_000 - reserved

    await credits.release_job("jobB")            # failed job → full refund
    assert await credits.get_balance(aid) == 10_000_000
    await credits.release_job("jobB")            # idempotent
    assert await credits.get_balance(aid) == 10_000_000


@pytest.mark.asyncio
async def test_failed_refund_leaves_reservation_held_for_retry(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    reserved = await _reserve(aid, "jobB2")
    original_credit = credits._credit_in_session

    async def boom(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(credits, "_credit_in_session", boom)
    await credits.release_job("jobB2")
    assert await credits.get_balance(aid) == 10_000_000 - reserved
    assert await _reservation_status("jobB2") == "held"

    monkeypatch.setattr(credits, "_credit_in_session", original_credit)
    await credits.release_job("jobB2")
    assert await credits.get_balance(aid) == 10_000_000
    assert await _reservation_status("jobB2") == "settled"


@pytest.mark.asyncio
async def test_settle_after_release_is_noop(db, monkeypatch):
    """Whoever reaches terminal first wins; the loser is a no-op."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    await _reserve(aid, "jobC")
    await credits.release_job("jobC")            # full refund first
    bal = await credits.get_balance(aid)
    await credits.settle_job("jobC", 500)        # too late — already settled
    assert await credits.get_balance(aid) == bal == 10_000_000


@pytest.mark.asyncio
async def test_open_reservation_idempotent_keeps_original(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    reserved = await _reserve(aid, "jobD")
    # A requeue re-opens with a different amount — must keep the ORIGINAL row.
    await credits.open_reservation("jobD", aid, PRICED, 999_999, 5)
    await credits.settle_job("jobD", 100)
    actual = pricing.quote_text(PRICED, 1000, 100)  # original prompt_toks=1000, not 5
    assert await credits.get_balance(aid) == 10_000_000 - actual


@pytest.mark.asyncio
async def test_settle_unknown_job_is_noop(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 1_000, "topup", ref="seed")
    await credits.settle_job("never-reserved", 100)   # no row → no-op
    await credits.release_job("never-reserved")
    assert await credits.get_balance(aid) == 1_000


@pytest.mark.asyncio
async def test_open_reservation_noop_in_dry_run(db, monkeypatch):
    """Ships dark: no reservation row is written when charging is off."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    aid = uuid.uuid4()
    await credits.open_reservation("jobE", aid, PRICED, 0, 100)
    # No row → settle is a no-op; balance untouched (there is none).
    await credits.settle_job("jobE", 50)
    assert await credits.get_balance(aid) == 0


# ── media: exact reserve → settle_exact stands; release refunds ──

IMG = "z-image-turbo"


@pytest.mark.asyncio
async def test_media_authorize_records_row_and_settle_exact_stands(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 1_000_000, "topup", ref="seed")
    auth = await credits.authorize_media(aid, IMG, "image", 2, None, "mjobA", record_reservation=True)
    cost = pricing.quote_image(IMG, 2)
    assert auth["ok"] and auth["reserved"] == cost and await _reservation_status("mjobA") == "held"
    assert await credits.get_balance(aid) == 1_000_000 - cost

    await credits.settle_exact("mjobA")               # success → exact charge stands
    assert await _reservation_status("mjobA") == "settled"
    assert await credits.get_balance(aid) == 1_000_000 - cost
    await credits.settle_exact("mjobA")               # idempotent
    assert await credits.get_balance(aid) == 1_000_000 - cost


@pytest.mark.asyncio
async def test_media_release_refunds_full(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 1_000_000, "topup", ref="seed")
    cost = (await credits.authorize_media(aid, IMG, "image", 1, None, "mjobB",
                                          record_reservation=True))["reserved"]
    assert await credits.get_balance(aid) == 1_000_000 - cost
    await credits.release_job("mjobB")                # failure → full refund
    assert await credits.get_balance(aid) == 1_000_000
    # settle_exact after release is a no-op (already settled)
    await credits.settle_exact("mjobB")
    assert await credits.get_balance(aid) == 1_000_000


@pytest.mark.asyncio
async def test_sweep_releases_stale_held(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    reserved = await _reserve(aid, "jobStale")
    assert await credits.get_balance(aid) == 10_000_000 - reserved
    # Nothing settled it (simulated crash). A sweep with threshold 0 releases it.
    n = await credits.sweep_stale_reservations(older_than_seconds=0)
    assert n == 1
    assert await credits.get_balance(aid) == 10_000_000
    assert await _reservation_status("jobStale") == "settled"
    # Fresh held reservation is NOT swept by a long threshold.
    await _reserve(aid, "jobFresh")
    assert await credits.sweep_stale_reservations(older_than_seconds=3600) == 0
