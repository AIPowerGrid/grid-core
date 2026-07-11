# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security and value-conservation tests for linked Grid identities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import identities
from grid_api.v2.schema import (
    account_aliases,
    account_identities,
    accounts,
    api_keys,
    credit_ledger,
    credits,
    metadata,
    promo_campaigns,
    promo_grants,
    reservations,
    workers,
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    try:
        yield engine
    finally:
        database._session_factory = old
        await engine.dispose()


async def _account(*, wallet=None, balance=0):
    account_id = uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(accounts).values(
            id=account_id, wallet=wallet, flags={}, created=datetime.now(timezone.utc),
        ))
        await session.execute(sa.insert(credits).values(
            account_id=account_id, balance_micro=balance,
        ))
        await session.commit()
    return account_id


@pytest.mark.asyncio
async def test_account_can_hold_multiple_verified_wallets(db):
    account_id = await _account()
    first = "0x" + "11" * 20
    second = "0x" + "22" * 20

    assert (await identities.attach_identity(account_id, "wallet", first))["status"] == "linked"
    assert (await identities.attach_identity(account_id, "wallet", second))["status"] == "linked"
    assert await identities.resolve_identity("wallet", first) == account_id
    assert await identities.resolve_identity("wallet", second) == account_id

    async with await database.new_session() as session:
        rows = (await session.execute(
            sa.select(account_identities.c.subject_hash, account_identities.c.is_primary)
            .where(account_identities.c.account_id == account_id)
        )).all()
        legacy_wallet = await session.scalar(
            sa.select(accounts.c.wallet).where(accounts.c.id == account_id)
        )
    assert len(rows) == 2
    assert sum(bool(row.is_primary) for row in rows) == 1
    assert legacy_wallet == second


def test_oauth_subjects_are_provider_specific_and_legacy_prefix_normalized():
    assert identities.subject_hash("google", "google_123") == identities.subject_hash("google", "123")
    assert identities.subject_hash("github", "github_123") == identities.subject_hash("github", "123")
    assert identities.subject_hash("google", "123") != identities.subject_hash("github", "123")


@pytest.mark.asyncio
async def test_identity_owned_by_another_account_is_not_stolen(db):
    owner = await _account()
    attacker = await _account()
    wallet = "0x" + "33" * 20
    await identities.attach_identity(owner, "wallet", wallet)

    result = await identities.attach_identity(attacker, "wallet", wallet)

    assert result == {
        "status": "conflict",
        "account_id": str(owner),
        "subject_hash": identities.subject_hash("wallet", wallet),
    }
    assert await identities.resolve_identity("wallet", wallet) == owner


@pytest.mark.asyncio
async def test_merge_conserves_purchased_credit_and_retires_source_access(db):
    destination = await _account(balance=70)
    source_wallet = "0x" + "44" * 20
    source = await _account(wallet=source_wallet, balance=30)
    source_key = "a" * 64
    worker_id = uuid4()
    async with await database.new_session() as session:
        await session.execute(sa.insert(api_keys).values(
            hash=source_key, account_id=source, scopes=["inference.submit"], revoked=False,
        ))
        await session.execute(sa.insert(workers).values(
            id=worker_id, account_id=source, name="merge-worker", type="text",
            models=[], capabilities={}, jobs_completed=0, den_earned=0,
        ))
        await session.commit()

    result = await identities.merge_accounts(
        destination, source, merge_ref="merge-credit-conservation",
    )

    assert result["status"] == "merged"
    assert await identities.canonical_account_id(source) == destination
    async with await database.new_session() as session:
        balances = dict((await session.execute(
            sa.select(credits.c.account_id, credits.c.balance_micro)
            .where(credits.c.account_id.in_([destination, source]))
        )).all())
        deltas = (await session.execute(
            sa.select(credit_ledger.c.account_id, credit_ledger.c.delta_micro,
                      credit_ledger.c.ref)
            .where(credit_ledger.c.ref.like("merge-credit-conservation:%"))
        )).all()
        key_revoked = await session.scalar(
            sa.select(api_keys.c.revoked).where(api_keys.c.hash == source_key)
        )
        worker_owner = await session.scalar(
            sa.select(workers.c.account_id).where(workers.c.id == worker_id)
        )
        source_payout = await session.scalar(
            sa.select(accounts.c.payout_wallet).where(accounts.c.id == source)
        )
        alias_count = await session.scalar(
            sa.select(sa.func.count()).select_from(account_aliases)
        )
    assert balances == {destination: 100, source: 0}
    assert sorted((row.delta_micro for row in deltas)) == [-30, 30]
    assert sum(row.delta_micro for row in deltas) == 0
    assert key_revoked is True
    assert worker_owner == destination
    assert source_payout == source_wallet  # already-accrued payouts remain payable
    assert alias_count == 1


@pytest.mark.asyncio
async def test_merge_does_not_multiply_same_campaign_grant(db):
    destination = await _account()
    source = await _account()
    now = datetime.now(timezone.utc)
    async with await database.new_session() as session:
        await session.execute(sa.insert(promo_campaigns).values(
            id="welcome", name="Welcome", grant_micro=150, granted_micro=300,
            eligibility={}, active=True, created=now,
        ))
        await session.execute(sa.insert(promo_grants), [
            {"id": uuid4(), "account_id": destination, "campaign_id": "welcome",
             "amount_micro": 150, "remaining_micro": 120, "status": "active",
             "ref": "welcome:destination", "expires": now + timedelta(days=30)},
            {"id": uuid4(), "account_id": source, "campaign_id": "welcome",
             "amount_micro": 150, "remaining_micro": 90, "status": "active",
             "ref": "welcome:source", "expires": now + timedelta(days=30)},
        ])
        await session.commit()

    await identities.merge_accounts(destination, source, merge_ref="merge-promo-cap")

    async with await database.new_session() as session:
        rows = (await session.execute(
            sa.select(promo_grants.c.account_id, promo_grants.c.remaining_micro,
                      promo_grants.c.status)
            .where(promo_grants.c.campaign_id == "welcome")
        )).all()
    assert sorted((row.remaining_micro for row in rows)) == [0, 120]
    assert sum(row.remaining_micro for row in rows) == 120
    assert {row.status for row in rows} == {"active", "merged"}


@pytest.mark.asyncio
async def test_alias_cycles_fail_closed(db):
    first = await _account()
    second = await _account()
    async with await database.new_session() as session:
        await session.execute(sa.insert(account_aliases), [
            {"source_account_id": first, "canonical_account_id": second,
             "merge_ref": "cycle:a", "reason": "test"},
            {"source_account_id": second, "canonical_account_id": first,
             "merge_ref": "cycle:b", "reason": "test"},
        ])
        await session.commit()

    with pytest.raises(RuntimeError, match="cycle"):
        await identities.canonical_account_id(first)


@pytest.mark.asyncio
async def test_merge_refuses_accounts_with_inflight_value(db):
    destination = await _account()
    source = await _account(balance=50)
    async with await database.new_session() as session:
        await session.execute(sa.insert(reservations).values(
            job_id="merge-inflight", account_id=source, model="test",
            reserved_micro=25, free_micro=0, promo_micro=0,
            prompt_toks=1, status="held",
        ))
        await session.commit()

    with pytest.raises(ValueError, match="in-flight"):
        await identities.merge_accounts(destination, source, merge_ref="must-not-merge")

    assert await identities.canonical_account_id(source) == source
