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
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import credits, deposits, identities, pricing, x402_payments
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import deposits as deposits_t
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import x402_payments as x402_payments_t


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
        *[credits.debit(aid, cost, "race", ref=f"race:{aid}:{i}") for i in range(n)],
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


@pytest.mark.asyncio
async def test_x402_authorization_cannot_open_multiple_jobs_under_race(pg, monkeypatch):
    payer = "0x1111111111111111111111111111111111111111"
    usdc = "0x2222222222222222222222222222222222222222"
    treasury = "0x3333333333333333333333333333333333333333"
    model = "gpt-oss-120b"
    maximum = pricing.quote_text(model, 100, 500)
    monkeypatch.setattr(x402_payments, "ENABLED", True)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "USDC", usdc)
    monkeypatch.setattr(x402_payments, "PAY_TO", treasury)
    payload = SimpleNamespace(
        payload={
            "permit2Authorization": {
                "from": payer,
                "nonce": "same-signed-permit",
            },
        },
    )
    requirements = SimpleNamespace(
        network="eip155:8453",
        asset=usdc,
        pay_to=treasury,
        amount=str(maximum),
    )

    results = await asyncio.gather(
        *[
            credits.authorize_x402_request(
                model,
                100,
                500,
                str(uuid.uuid4()),
                payment_payload=payload,
                payment_requirements=requirements,
            )
            for _ in range(20)
        ],
    )

    assert sum(1 for result in results if result["ok"]) == 1, results
    assert sum(1 for result in results if result["status"] == "conflict") == 19, results


@pytest.mark.asyncio
async def test_x402_settlement_attempt_is_claimed_once_under_race(pg, monkeypatch):
    payer = "0x1111111111111111111111111111111111111111"
    usdc = "0x2222222222222222222222222222222222222222"
    treasury = "0x3333333333333333333333333333333333333333"
    model = "gpt-oss-120b"
    job_id = str(uuid.uuid4())
    maximum = pricing.quote_text(model, 100, 500)
    actual = pricing.quote_text(model, 100, 50)
    monkeypatch.setattr(x402_payments, "ENABLED", True)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "USDC", usdc)
    monkeypatch.setattr(x402_payments, "PAY_TO", treasury)
    payload = SimpleNamespace(
        payload={"permit2Authorization": {"from": payer, "nonce": "one-attempt"}},
    )
    requirements = SimpleNamespace(
        network="eip155:8453",
        asset=usdc,
        pay_to=treasury,
        amount=str(maximum),
    )
    assert (
        await credits.authorize_x402_request(
            model,
            100,
            500,
            job_id,
            payment_payload=payload,
            payment_requirements=requirements,
        )
    )["ok"]

    context = SimpleNamespace(
        requirements=SimpleNamespace(amount=str(actual)),
        transport_context=SimpleNamespace(
            response_headers={"X-Grid-Job-ID": job_id},
        ),
    )
    results = await asyncio.gather(
        *[x402_payments._before_settle(context) for _ in range(20)],
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, RuntimeError) for result in results) == 19

    async with await database.new_session() as session:
        row = (
            await session.execute(
                sa.select(
                    x402_payments_t.c.status,
                    x402_payments_t.c.settled_micro,
                    x402_payments_t.c.attempts,
                ).where(x402_payments_t.c.job_id == job_id),
            )
        ).one()
    assert row == ("settling", actual, 1)


@pytest.mark.asyncio
async def test_usdc_daily_caps_hold_under_concurrent_deposits(pg, monkeypatch):
    aid = await _seed_account()
    wallet = "0x1111111111111111111111111111111111111111"
    treasury = "0x2222222222222222222222222222222222222222"
    token = "0x3333333333333333333333333333333333333333"
    per_deposit = 1_000
    allowed = 5
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)

    async def fund(index: int):
        return await deposits._record_and_credit(
            account={"account_id": aid, "wallet": wallet},
            asset="USDC",
            token_address=token,
            tx_hash="0x" + f"{index:064x}",
            block_number=100 + index,
            sender=wallet,
            treasury=treasury,
            amount_raw=per_deposit,
            decimals=6,
            price_micro=1_000_000,
            price_source="usdc:1:1",
            price_timestamp=datetime.now(UTC),
            price_block=100 + index,
            credited_micro=per_deposit,
            caps=(per_deposit, per_deposit * allowed, per_deposit * allowed),
        )

    results = await asyncio.gather(
        *[fund(index) for index in range(20)],
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) and result[0] for result in results) == allowed
    assert sum(isinstance(result, HTTPException) for result in results) == 20 - allowed
    assert await credits.get_balance(aid) == per_deposit * allowed
    async with await database.new_session() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(deposits_t))
    assert count == allowed
