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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, pricing
from grid_api.v2.schema import metadata as v2_metadata

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
    """Reserve at max + open the durable row, as the request path does."""
    auth = await credits.authorize_request({"account_id": aid}, PRICED, prompt, mx, job_id)
    assert auth["ok"]
    await credits.open_reservation(job_id, aid, PRICED, auth["reserved"], prompt)
    return auth["reserved"]


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
