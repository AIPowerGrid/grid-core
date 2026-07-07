# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free-first charging in the LIVE durable reserve path (the charging go-live gate).

The auditor's blocking scenario: a user with free credit but no paid balance
must NOT be 402'd once charging flips on. These tests drive the real
authorize_request / settle_job / record_and_settle code (in-memory sqlite) with
a semantically-faithful fake free store (atomic consume/release, idempotent on
ref, day-cap accounting) — proving the two-pocket invariant: free restores to
free, paid refunds to paid, and the pockets never convert."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sqlalchemy as sa

from grid_api import database
from grid_api.services import credits, free_credits, pricing
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import reservations as reservations_t

PRICED = "gpt-oss-120b"


class FakeFree:
    """In-memory mirror of the Redis free bucket: cap/day, idempotent on ref."""

    def __init__(self, cap):
        self.cap = cap
        self.spent = 0
        self.refs: dict[str, int] = {}

    async def consume(self, aid, wallet, want, ref):
        if ref in self.refs:
            return self.refs[ref]
        take = max(min(int(want), self.cap - self.spent), 0)
        self.spent += take
        self.refs[ref] = take
        return take

    async def release(self, aid, ref, keep_micro=0):
        consumed = self.refs.get(str(ref), self.refs.get(ref, 0))
        delta = consumed - int(keep_micro)
        if delta <= 0:
            return 0
        self.spent = max(self.spent - delta, 0)
        self.refs[str(ref)] = int(keep_micro)
        return delta


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.fixture
def live(monkeypatch):
    """Charging live + free spendable, with a controllable fake free store."""
    def _wire(cap):
        fake = FakeFree(cap)
        monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
        monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
        monkeypatch.setattr(free_credits, "FREE_SPENDABLE_LIVE", True)
        monkeypatch.setattr(free_credits, "consume", fake.consume)
        monkeypatch.setattr(free_credits, "release", fake.release)

        async def _no_wallet(aid):
            return None
        monkeypatch.setattr(credits, "_wallet_for_account", _no_wallet)
        return fake
    return _wire


def _aid():
    # A real UUID object, as resolve_api_key hands the live path (sqlite's Uuid
    # column requires it; asyncpg would coerce a string, sqlite won't).
    return uuid.uuid4()


async def _reservation(job_id):
    async with await database.new_session() as s:
        r = (await s.execute(
            sa.select(reservations_t.c.reserved_micro, reservations_t.c.free_micro,
                      reservations_t.c.status).where(reservations_t.c.job_id == str(job_id))
        )).first()
        return {"reserved": int(r[0]), "free": int(r[1]), "status": r[2]} if r else None


def _ledger_values(job_id):
    return dict(job_id=job_id, worker_id=str(uuid.uuid4()), wallet="0x" + "1" * 40,
                model=PRICED, job_type="text", den=1.0, output_units=100,
                duration=1.0, ttft=0.1, prompt_hash="ph", result_hash="rh")


@pytest.mark.asyncio
async def test_free_user_with_no_paid_balance_is_not_402d(db, live):
    """THE go-live gate: free covers the whole reserve; zero paid balance; ok."""
    cost = pricing.quote_text(PRICED, 100, 200)
    fake = live(cap=cost + 10_000)
    aid, job = _aid(), str(uuid.uuid4())
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 200, job,
                                          record_reservation=True)
    assert out["ok"], out
    assert out["from_free"] == cost
    row = await _reservation(job)
    assert row == {"reserved": cost, "free": cost, "status": "held"}
    assert await credits.get_balance(aid) == 0  # paid untouched (there was none)
    assert fake.spent == cost


@pytest.mark.asyncio
async def test_free_covers_part_paid_holds_remainder(db, live):
    cost = pricing.quote_text(PRICED, 100, 200)
    free_cap = max(cost // 3, 1)  # free covers only a slice of the cost
    fake = live(cap=free_cap)
    aid, job = _aid(), str(uuid.uuid4())
    await credits.credit(aid, 1_000_000, "test:seed", ref="seed1")
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 200, job,
                                          record_reservation=True)
    assert out["ok"] and out["from_free"] == free_cap
    assert (await _reservation(job))["free"] == free_cap
    assert await credits.get_balance(aid) == 1_000_000 - (cost - free_cap)
    assert fake.spent == free_cap


@pytest.mark.asyncio
async def test_insufficient_paid_releases_the_free_it_took(db, live):
    cost = pricing.quote_text(PRICED, 100, 200)
    fake = live(cap=max(cost // 3, 1))  # free can't cover it; no paid at all
    aid, job = _aid(), str(uuid.uuid4())
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 200, job,
                                          record_reservation=True)  # no paid at all
    assert not out["ok"] and out["status"] == "insufficient"
    assert fake.spent == 0  # the free draw was rolled back
    assert await _reservation(job) is None


@pytest.mark.asyncio
async def test_settle_underrun_restores_free_first_then_paid(db, live):
    """actual < free_held: only `actual` stays consumed from free; the ENTIRE
    paid hold refunds. Free never converts to paid or vice versa."""
    reserve_cost = pricing.quote_text(PRICED, 100, 1000)
    free_cap = reserve_cost // 2
    fake = live(cap=free_cap)
    aid, job = _aid(), str(uuid.uuid4())
    await credits.credit(aid, 5_000_000, "test:seed", ref="seed2")
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 1000, job,
                                          record_reservation=True)
    assert out["ok"] and out["from_free"] == free_cap
    paid_held = reserve_cost - free_cap

    # settle at a tiny actual, below the free portion
    small_out = 1
    actual = pricing.quote_text(PRICED, 100, small_out)
    assert actual < free_cap
    st = await credits.record_and_settle(ledger_values=_ledger_values(job),
                                         completion_tokens=small_out)
    assert st == "settled"
    # free keeps only `actual` consumed; paid hold fully refunded
    assert fake.spent == actual
    assert await credits.get_balance(aid) == 5_000_000  # paid_held refunded in full
    assert (await _reservation(job))["status"] == "settled"


@pytest.mark.asyncio
async def test_settle_between_free_and_reserved(db, live):
    """free_held < actual < reserved: free stays fully consumed; paid refunds
    exactly reserved - actual."""
    reserve_cost = pricing.quote_text(PRICED, 100, 1000)
    mid_out = 500
    actual = pricing.quote_text(PRICED, 100, mid_out)
    free_cap = max(actual // 2, 1)  # strictly below the eventual actual
    fake = live(cap=free_cap)
    aid, job = _aid(), str(uuid.uuid4())
    await credits.credit(aid, 5_000_000, "test:seed", ref="seed3")
    await credits.authorize_request({"account_id": aid}, PRICED, 100, 1000, job,
                                    record_reservation=True)
    assert free_cap < actual < reserve_cost
    st = await credits.record_and_settle(ledger_values=_ledger_values(job),
                                         completion_tokens=mid_out)
    assert st == "settled"
    assert fake.spent == free_cap  # free portion fully consumed
    expected_balance = 5_000_000 - (reserve_cost - free_cap) + (reserve_cost - actual)
    assert await credits.get_balance(aid) == expected_balance


@pytest.mark.asyncio
async def test_failed_job_restores_both_pockets(db, live):
    cost = pricing.quote_text(PRICED, 100, 500)
    free_cap = max(cost // 2, 1)
    fake = live(cap=free_cap)
    aid, job = _aid(), str(uuid.uuid4())
    await credits.credit(aid, 2_000_000, "test:seed", ref="seed4")
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 500, job,
                                          record_reservation=True)
    assert out["ok"] and out["from_free"] == free_cap
    await credits.release_job(job)  # settle_job(status='failed')
    assert fake.spent == 0                                # free fully restored
    assert await credits.get_balance(aid) == 2_000_000    # paid fully refunded
    assert (await _reservation(job))["status"] == "settled"


@pytest.mark.asyncio
async def test_spendable_flag_off_means_paid_only(db, live, monkeypatch):
    """GRID_FREE_SPENDABLE_LIVE off → free is display-only; the whole cost holds
    from paid (matches what /v1/account/credits reports as total_spendable)."""
    fake = live(cap=10_000_000)
    monkeypatch.setattr(free_credits, "FREE_SPENDABLE_LIVE", False)
    cost = pricing.quote_text(PRICED, 100, 200)
    aid, job = _aid(), str(uuid.uuid4())
    await credits.credit(aid, 1_000_000, "test:seed", ref="seed5")
    out = await credits.authorize_request({"account_id": aid}, PRICED, 100, 200, job,
                                          record_reservation=True)
    assert out["ok"] and out["from_free"] == 0
    assert fake.spent == 0
    assert await credits.get_balance(aid) == 1_000_000 - cost
