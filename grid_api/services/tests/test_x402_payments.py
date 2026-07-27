# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""x402 authorization, usage settlement, and worker-payout gating."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, pricing, x402_payments
from grid_api.services.settlement.aggregate import aggregate_den_by_account
from grid_api.v2.schema import accounts, credit_ledger, ledger, metadata, reservations, workers
from grid_api.v2.schema import x402_payments as payments

MODEL = "gpt-oss-120b"
PAYER = "0x1111111111111111111111111111111111111111"
USDC = "0x2222222222222222222222222222222222222222"
TREASURY = "0x3333333333333333333333333333333333333333"


@pytest_asyncio.fixture
async def db():
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
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(x402_payments, "ENABLED", True)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "USDC", USDC)
    monkeypatch.setattr(x402_payments, "PAY_TO", TREASURY)


def _payment(amount: int):
    payload = SimpleNamespace(
        payload={"permit2Authorization": {"from": PAYER, "nonce": "12345"}},
    )
    requirements = SimpleNamespace(
        network="eip155:8453",
        asset=USDC,
        pay_to=TREASURY,
        amount=str(amount),
    )
    return payload, requirements


async def _rows(table):
    async with await database.new_session() as session:
        return (await session.execute(sa.select(table))).all()


def test_cdp_headers_are_short_lived_and_bound_to_each_endpoint(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    secret = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_ID", "organizations/test/apiKeys/key")
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_SECRET", secret)
    monkeypatch.setattr(
        x402_payments,
        "FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )

    headers = x402_payments._facilitator_headers()
    verify_token = headers["verify"]["Authorization"].removeprefix("Bearer ")
    settle_token = headers["settle"]["Authorization"].removeprefix("Bearer ")
    verify_claims = jwt.decode(
        verify_token,
        options={"verify_signature": False, "verify_aud": False},
    )
    settle_claims = jwt.decode(
        settle_token,
        options={"verify_signature": False, "verify_aud": False},
    )

    assert verify_claims["uris"] == ["POST api.cdp.coinbase.com/platform/v2/x402/verify"]
    assert settle_claims["uris"] == ["POST api.cdp.coinbase.com/platform/v2/x402/settle"]
    assert 0 < verify_claims["exp"] - verify_claims["nbf"] <= 120


def test_mainnet_config_fails_closed_without_facilitator_credentials(monkeypatch):
    monkeypatch.setattr(x402_payments, "ENABLED", True)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "PAY_TO", TREASURY)
    monkeypatch.setattr(x402_payments, "USDC", USDC)
    monkeypatch.setattr(
        x402_payments,
        "FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_ID", "")
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_SECRET", "")

    with pytest.raises(RuntimeError, match="requires CDP"):
        x402_payments.validate_config()


@pytest.mark.asyncio
async def test_external_reservation_is_atomic_and_never_touches_credit_ledger(db, enabled):
    job_id = str(uuid.uuid4())
    maximum = pricing.quote_text(MODEL, 100, 500)
    payload, requirements = _payment(maximum)

    result = await credits.authorize_x402_request(
        MODEL,
        100,
        500,
        job_id,
        payment_payload=payload,
        payment_requirements=requirements,
    )

    assert result == {
        "ok": True,
        "reserved": maximum,
        "status": "ok",
        "payer": PAYER,
    }
    reservation = (await _rows(reservations))[0]
    receipt = (await _rows(payments))[0]
    assert reservation.billing_source == "x402"
    assert reservation.external_payer == PAYER
    assert receipt.status == "verified"
    assert receipt.authorized_micro == maximum
    assert await _rows(credit_ledger) == []


@pytest.mark.asyncio
async def test_under_authorized_request_writes_nothing(db, enabled):
    job_id = str(uuid.uuid4())
    payload, requirements = _payment(1)

    result = await credits.authorize_x402_request(
        MODEL,
        100,
        500,
        job_id,
        payment_payload=payload,
        payment_requirements=requirements,
    )

    assert result["ok"] is False
    assert result["status"] == "authorization_too_small"
    assert await _rows(reservations) == []
    assert await _rows(payments) == []


@pytest.mark.asyncio
async def test_one_authorization_cannot_open_two_jobs(db, enabled):
    maximum = pricing.quote_text(MODEL, 100, 500)
    payload, requirements = _payment(maximum)
    first = await credits.authorize_x402_request(
        MODEL,
        100,
        500,
        str(uuid.uuid4()),
        payment_payload=payload,
        payment_requirements=requirements,
    )
    second = await credits.authorize_x402_request(
        MODEL,
        100,
        500,
        str(uuid.uuid4()),
        payment_payload=payload,
        payment_requirements=requirements,
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["status"] == "conflict"
    assert len(await _rows(reservations)) == 1
    assert len(await _rows(payments)) == 1


@pytest.mark.asyncio
async def test_database_rejects_settlement_above_authorization(db):
    async with await database.new_session() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.insert(payments).values(
                    job_id=str(uuid.uuid4()),
                    authorization_id="a" * 64,
                    payer=PAYER,
                    network="eip155:8453",
                    asset=USDC,
                    pay_to=TREASURY,
                    authorized_micro=100,
                    settled_micro=101,
                    status="settled",
                    created=datetime.now(UTC),
                    settled=datetime.now(UTC),
                ),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_actual_cost_is_recorded_and_payout_waits_for_onchain_settlement(db, enabled):
    account_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts).values(
                id=account_id,
                wallet=PAYER,
                payout_wallet=PAYER,
                flags={},
                created=now,
            ),
        )
        await session.execute(
            sa.insert(workers).values(
                id=worker_id,
                account_id=account_id,
                name="x402-test-worker",
                type="text",
                wallet=PAYER,
                models=[MODEL],
                capabilities={},
                first_seen=now,
                jobs_completed=0,
                den_earned=0,
            ),
        )
        await session.commit()

    maximum = pricing.quote_text(MODEL, 100, 500)
    payload, requirements = _payment(maximum)
    assert (
        await credits.authorize_x402_request(
            MODEL,
            100,
            500,
            job_id,
            payment_payload=payload,
            payment_requirements=requirements,
        )
    )["ok"]

    terminal = await credits.record_and_settle(
        ledger_values={
            "job_id": job_id,
            "worker_id": str(worker_id),
            "wallet": PAYER,
            "model": MODEL,
            "job_type": "text",
            "den": 25.0,
            "output_units": 50,
            "prompt_hash": None,
            "result_hash": None,
        },
        completion_tokens=50,
    )
    assert terminal == "settled"
    actual = pricing.quote_text(MODEL, 100, 50)
    assert await credits.reservation_actual_micro(job_id) == actual
    assert len(await _rows(ledger)) == 1

    before = await aggregate_den_by_account(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert before == []

    async with await database.new_session() as session:
        await session.execute(
            sa.update(payments)
            .where(payments.c.job_id == job_id)
            .values(
                status="settled",
                settled_micro=actual,
                tx_hash="0x" + "ab" * 32,
                settled=now,
            ),
        )
        await session.commit()

    after = await aggregate_den_by_account(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert after == [
        {
            "account_id": str(account_id),
            "den": 25.0,
            "payout_address": PAYER,
        },
    ]
