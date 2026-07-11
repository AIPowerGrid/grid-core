# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import promotions
from grid_api.v2.schema import accounts, metadata, promo_campaigns, promo_grants, promo_spends


@pytest_asyncio.fixture
async def promo_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(promotions, "PROMO_ENABLED", True)
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", True)
    aid = uuid4()
    async with database._session_factory() as session:
        await session.execute(sa.insert(accounts).values(id=aid, flags={}, username="promo-test"))
        await session.execute(sa.insert(promo_campaigns).values(
            id="welcome-test", name="Welcome", grant_micro=150_000,
            budget_micro=300_000, granted_micro=0, expires_days=30,
            eligibility={"verified_identity": True}, active=True,
            created=datetime.now(timezone.utc),
        ))
        await session.commit()
    try:
        yield aid
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.mark.asyncio
async def test_grant_once_is_idempotent_and_budgeted(promo_db):
    first = await promotions.grant_once(promo_db, "welcome-test")
    second = await promotions.grant_once(promo_db, "welcome-test")
    assert first["status"] == "granted" and first["granted_micro"] == 150_000
    assert second["status"] == "already" and second["remaining_micro"] == 150_000
    async with await database.new_session() as session:
        campaign_total = await session.scalar(
            sa.select(promo_campaigns.c.granted_micro).where(promo_campaigns.c.id == "welcome-test")
        )
        grant_count = await session.scalar(sa.select(sa.func.count()).select_from(promo_grants))
    assert campaign_total == 150_000
    assert grant_count == 1


@pytest.mark.asyncio
async def test_consume_and_release_restore_same_grant(promo_db):
    await promotions.grant_once(promo_db, "welcome-test")
    assert await promotions.consume(promo_db, 100_000, "job-1") == 100_000
    assert await promotions.consume(promo_db, 100_000, "job-1") == 100_000
    assert await promotions.available_micro(promo_db) == 50_000

    restored = await promotions.release(promo_db, "job-1", keep_micro=40_000)
    assert restored == 60_000
    assert await promotions.available_micro(promo_db) == 110_000
    assert await promotions.release(promo_db, "job-1", keep_micro=40_000) == 0
    async with await database.new_session() as session:
        spend = (await session.execute(
            sa.select(promo_spends.c.kept_micro, promo_spends.c.status)
            .where(promo_spends.c.ref == "job-1")
        )).first()
    assert spend == (40_000, "settled")


@pytest.mark.asyncio
async def test_expired_grant_is_not_spendable_or_restored(promo_db):
    await promotions.grant_once(promo_db, "welcome-test")
    assert await promotions.consume(promo_db, 50_000, "job-expired") == 50_000
    async with await database.new_session() as session:
        await session.execute(
            sa.update(promo_grants).values(expires=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()
    assert await promotions.available_micro(promo_db) == 0
    assert await promotions.release(promo_db, "job-expired", keep_micro=0) == 0


@pytest.mark.asyncio
async def test_shadow_flag_never_consumes(promo_db, monkeypatch):
    await promotions.grant_once(promo_db, "welcome-test")
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", False)
    assert await promotions.consume(promo_db, 150_000, "shadow-job") == 0
    assert await promotions.available_micro(promo_db) == 150_000


@pytest.mark.asyncio
async def test_sweeper_restores_orphaned_promo_hold(promo_db):
    await promotions.grant_once(promo_db, "welcome-test")
    assert await promotions.consume(promo_db, 80_000, "orphan-job") == 80_000
    async with await database.new_session() as session:
        await session.execute(
            sa.update(promo_spends)
            .where(promo_spends.c.ref == "orphan-job")
            .values(created=datetime.now(timezone.utc) - timedelta(hours=2))
        )
        await session.commit()

    assert await promotions.sweep_stale_spends(older_than_seconds=3600) == 1
    assert await promotions.available_micro(promo_db) == 150_000
