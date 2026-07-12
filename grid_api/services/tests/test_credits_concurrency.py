# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres concurrency proof for the overdraft-safe debit.

test_credits_billing.py proves the conditional-UPDATE LOGIC on SQLite/StaticPool
(serialized writes). This proves the same invariant under TRUE Postgres row-lock
concurrency: many debits racing a balance that covers only some must never
overdraw. Skipped unless CREDITS_TEST_DB_URL points at a real Postgres.
"""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import credits, identities
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata as v2_metadata


async def _seed_account() -> uuid.UUID:
    """Create a real grid_accounts row (PG enforces the credit_ledger FK — unlike
    SQLite, where it silently didn't)."""
    aid = uuid.uuid4()
    async with await database.new_session() as s:
        await s.execute(sa.insert(accounts_t).values(id=aid))
        await s.commit()
    return aid

_PG = os.environ.get("CREDITS_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not _PG.startswith("postgresql"),
    reason="set CREDITS_TEST_DB_URL=postgresql+asyncpg://… for the real-PG row-lock test",
)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(_PG)  # default pool → real concurrent connections
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine
    finally:
        database._session_factory = old
        async with engine.begin() as conn:
            await conn.run_sync(v2_metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_debits_never_overdraft(pg):
    aid = await _seed_account()
    cost = 1_000            # micro-USD per debit
    covered = 5            # balance covers exactly this many
    n = 25                 # racers (5x over-subscribed)

    assert await credits.credit(aid, cost * covered, "seed", ref=f"seed:{aid}")

    results = await asyncio.gather(
        *[credits.debit(aid, cost, "race", ref=f"race:{aid}:{i}") for i in range(n)]
    )

    oks = sum(1 for r in results if r == "ok")
    insufficient = sum(1 for r in results if r == "insufficient")
    balance = await credits.get_balance(aid)

    # Exactly `covered` win; the rest are cleanly rejected; balance never negative.
    assert oks == covered, (oks, results)
    assert insufficient == n - covered, (insufficient, results)
    assert balance == 0, balance


@pytest.mark.asyncio
async def test_duplicate_ref_debit_charges_once_under_race(pg):
    # The same ref fired concurrently must debit exactly once (idempotency on ref).
    aid = await _seed_account()
    cost = 1_000
    assert await credits.credit(aid, cost * 10, "seed", ref=f"seed:{aid}")

    ref = f"dup:{aid}"
    results = await asyncio.gather(*[credits.debit(aid, cost, "dup", ref=ref) for _ in range(12)])

    assert sum(1 for r in results if r == "ok") == 1, results
    assert sum(1 for r in results if r == "already") == 11, results
    assert await credits.get_balance(aid) == cost * 9


@pytest.mark.asyncio
async def test_opposing_account_merges_cannot_create_alias_cycle(pg):
    first = await _seed_account()
    second = await _seed_account()
    results = await asyncio.gather(
        identities.merge_accounts(first, second, merge_ref=f"merge:{first}"),
        identities.merge_accounts(second, first, merge_ref=f"merge:{second}"),
        return_exceptions=True,
    )
    assert not all(isinstance(result, Exception) for result in results)
    assert await identities.canonical_account_id(first) == await identities.canonical_account_id(second)


@pytest.mark.asyncio
async def test_credit_racing_merge_is_not_stranded_on_retired_account(pg):
    destination = await _seed_account()
    source = await _seed_account()
    await asyncio.gather(
        credits.credit(source, 50_000, "deposit", ref=f"deposit:{source}"),
        identities.merge_accounts(destination, source, merge_ref=f"credit-race:{source}"),
    )
    canonical = await identities.canonical_account_id(source)
    assert canonical == await identities.canonical_account_id(destination)
    assert await credits.get_balance(canonical) == 50_000
