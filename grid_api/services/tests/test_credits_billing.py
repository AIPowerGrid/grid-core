# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DB-backed money-invariant tests for Phase 1 billing (reserve/reconcile).

Runs against an in-memory SQLite (StaticPool → one shared connection) with the
v2 schema created, with `database._session_factory` pointed at it. Proves the
invariants the audit demanded:

  * `ref` required (null rejected) for value-moving rows
  * credit/debit idempotent on `ref`
  * debit is overdraft-safe (conditional UPDATE refuses the overdraft)
  * authorize_request blocks insufficient balance (→ caller 402 before dispatch)
  * unpriced model blocked in enforce mode
  * reserve-then-refund settles to actual usage
  * dry-run authorize is a no-op

Caveat: SQLite/StaticPool serializes writes, so this proves the *conditional
UPDATE logic* refuses an overdraft, not true Postgres row-lock concurrency —
that belongs in an integration test against Postgres.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, pricing
from grid_api.v2.schema import metadata as v2_metadata

PRICED = "gpt-oss-120b"  # text, in the price book
IMG = "z-image-turbo"    # image, priced
VID = "ltx-2.3"          # video, priced per second


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
    database._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.mark.asyncio
async def test_null_ref_rejected(db):
    aid = uuid.uuid4()
    with pytest.raises(ValueError):
        await credits.credit(aid, 1000, "topup", ref=None)
    with pytest.raises(ValueError):
        await credits.debit(aid, 1000, "spend", ref=None)


@pytest.mark.asyncio
async def test_credit_idempotent_on_ref(db):
    aid = uuid.uuid4()
    assert await credits.credit(aid, 5000, "topup", ref="r1") is True
    assert await credits.credit(aid, 5000, "topup", ref="r1") is False  # dup ref
    assert await credits.get_balance(aid) == 5000


@pytest.mark.asyncio
async def test_debit_idempotent_and_overdraft_safe(db):
    aid = uuid.uuid4()
    await credits.credit(aid, 1000, "topup", ref="seed")
    assert await credits.debit(aid, 600, "spend", ref="d1") == "ok"
    assert await credits.debit(aid, 600, "spend", ref="d1") == "already"  # idempotent
    assert await credits.get_balance(aid) == 400
    # a DISTINCT debit that would overdraft is refused; balance untouched
    assert await credits.debit(aid, 600, "spend", ref="d2") == "insufficient"
    assert await credits.get_balance(aid) == 400


@pytest.mark.asyncio
async def test_authorize_blocks_insufficient(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 5, "topup", ref="seed")  # 5 micro-USD — tiny
    auth = await credits.authorize_request({"account_id": aid}, PRICED, 1000, 1000, "job1")
    assert auth["ok"] is False
    assert auth["status"] == "insufficient"
    assert await credits.get_balance(aid) == 5  # nothing reserved


@pytest.mark.asyncio
async def test_authorize_unpriced_blocked_in_enforce(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    auth = await credits.authorize_request(
        {"account_id": uuid.uuid4()}, "totally-unknown-model-xyz", 10, 10, "job2"
    )
    assert auth["ok"] is False
    assert auth["status"] == "unpriced"


@pytest.mark.asyncio
async def test_authorize_no_account_blocked_in_enforce(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    auth = await credits.authorize_request({}, PRICED, 10, 10, "job3")  # legacy / no account_id
    assert auth["ok"] is False
    assert auth["status"] == "no_account"


@pytest.mark.asyncio
async def test_reserve_then_refund_settles_to_actual(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    user = {"account_id": aid}
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    # Reserve at MAX (prompt 1000, max_tokens 1000)
    auth = await credits.authorize_request(user, PRICED, 1000, 1000, "job4")
    assert auth["ok"] and auth["reserved"] > 0
    reserved = auth["reserved"]
    assert await credits.get_balance(aid) == 10_000_000 - reserved
    # Actual completion was smaller → refund the difference
    await credits.reconcile(user, PRICED, 1000, 100, reserved, "job4")
    actual = pricing.quote_text(PRICED, 1000, 100)
    assert await credits.get_balance(aid) == 10_000_000 - actual
    # reconcile is idempotent on the :refund ref
    await credits.reconcile(user, PRICED, 1000, 100, reserved, "job4")
    assert await credits.get_balance(aid) == 10_000_000 - actual


@pytest.mark.asyncio
async def test_authorize_noop_in_dry_run(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    auth = await credits.authorize_request({"account_id": uuid.uuid4()}, PRICED, 10, 10, "j")
    assert auth["ok"] and auth["reserved"] == 0 and auth["status"] == "dry_run"


# ── Media (B4): same reserve/refund path ──


@pytest.mark.asyncio
async def test_authorize_media_image_reserves_n(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 1_000_000, "topup", ref="seed")
    auth = await credits.authorize_media(aid, IMG, "image", 2, None, "mjob1")
    cost = pricing.quote_image(IMG, 2)
    assert auth["ok"] and cost > 0 and auth["reserved"] == cost
    assert await credits.get_balance(aid) == 1_000_000 - cost


@pytest.mark.asyncio
async def test_authorize_media_video_reserves_seconds(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 10_000_000, "topup", ref="seed")
    auth = await credits.authorize_media(aid, VID, "video", 1, 5, "mjob2")
    cost = pricing.quote_video(VID, 5)
    assert auth["ok"] and cost > 0 and auth["reserved"] == cost


@pytest.mark.asyncio
async def test_authorize_media_unpriced_blocked(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    auth = await credits.authorize_media(uuid.uuid4(), "no-such-image-xyz", "image", 1, None, "mjob3")
    assert auth["ok"] is False and auth["status"] == "unpriced"


@pytest.mark.asyncio
async def test_authorize_media_insufficient_blocked(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 5, "topup", ref="seed")
    auth = await credits.authorize_media(aid, IMG, "image", 1, None, "mjob4")
    assert auth["ok"] is False and auth["status"] == "insufficient"
    assert await credits.get_balance(aid) == 5


@pytest.mark.asyncio
async def test_media_refund_on_failure_idempotent(db, monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    aid = uuid.uuid4()
    await credits.credit(aid, 1_000_000, "topup", ref="seed")
    cost = (await credits.authorize_media(aid, IMG, "image", 1, None, "mjob5"))["reserved"]
    assert await credits.get_balance(aid) == 1_000_000 - cost
    await credits.refund_reservation(aid, cost, "mjob5")
    assert await credits.get_balance(aid) == 1_000_000
    await credits.refund_reservation(aid, cost, "mjob5")  # idempotent
    assert await credits.get_balance(aid) == 1_000_000
