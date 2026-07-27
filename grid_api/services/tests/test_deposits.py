# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DB-backed Base funding receipt and credit invariants."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import credits, deposits
from grid_api.v2.schema import accounts, credit_ledger, metadata
from grid_api.v2.schema import deposits as deposits_t

WALLET = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
TREASURY = "0x3333333333333333333333333333333333333333"
USDC = "0x4444444444444444444444444444444444444444"
AIPG = "0x5555555555555555555555555555555555555555"
ROUTER = "0x6666666666666666666666666666666666666666"
POOL = "0x7777777777777777777777777777777777777777"
TX = "0x" + "ab" * 32


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
    account_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts).values(
                id=account_id,
                wallet=WALLET,
                flags={},
                created=datetime.now(UTC),
            ),
        )
        await session.commit()
    try:
        yield account_id
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.fixture
def funding(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    monkeypatch.setattr(deposits, "DEPOSITS_ENABLED", True)
    monkeypatch.setattr(deposits, "TREASURY", TREASURY)
    monkeypatch.setattr(deposits, "USDC", USDC)
    monkeypatch.setattr(deposits, "USDC_MAX_DEPOSIT_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "USDC_ACCOUNT_DAILY_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "USDC_NETWORK_DAILY_MICRO", 500_000_000)
    monkeypatch.setattr(deposits, "AIPG_ENABLED", True)
    monkeypatch.setattr(deposits, "AIPG_TREASURY", TREASURY)
    monkeypatch.setattr(deposits, "AIPG_TOKEN", AIPG)
    monkeypatch.setattr(deposits, "AIPG_PRICE_MICRO", 2_000)
    monkeypatch.setattr(deposits, "AIPG_PRICE_EPOCH", "test-epoch")
    monkeypatch.setattr(deposits, "AIPG_PRICE_AS_OF_RAW", (now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(deposits, "AIPG_PRICE_VALID_UNTIL_RAW", (now + timedelta(hours=1)).isoformat())
    monkeypatch.setattr(deposits, "AIPG_PRICE_MAX_AGE_SECONDS", 86_400)
    monkeypatch.setattr(deposits, "AIPG_PRICE_BLOCK", 123)
    monkeypatch.setattr(deposits, "AIPG_HAIRCUT_BPS", 300)
    monkeypatch.setattr(deposits, "AIPG_MAX_DEPOSIT_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "AIPG_ACCOUNT_DAILY_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "AIPG_NETWORK_DAILY_MICRO", 500_000_000)
    monkeypatch.setattr(deposits, "ETH_CONVERSION_MODE", "disabled")
    monkeypatch.setattr(deposits, "CONFIRMATIONS", 3)
    monkeypatch.setattr(deposits, "MIN_CREDIT_MICRO", 10_000)


def _topic(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def _transfer_log(token: str, sender: str, recipient: str, amount: int) -> dict:
    return {
        "address": token,
        "topics": [
            deposits._TRANSFER_TOPIC,
            _topic(sender),
            _topic(recipient),
        ],
        "data": hex(amount),
    }


def _rpc_for(token: str, amount: int, *, sender: str = WALLET):
    transaction = {"hash": TX, "from": sender, "to": token, "value": "0x0"}
    receipt = {
        "status": "0x1",
        "blockNumber": hex(100),
        "logs": [_transfer_log(token, sender, TREASURY, amount)],
    }

    async def rpc(method, _params):
        return {
            "eth_chainId": hex(8453),
            "eth_getTransactionByHash": transaction,
            "eth_getTransactionReceipt": receipt,
            "eth_blockNumber": hex(102),
            "eth_getBlockByNumber": {"timestamp": hex(int(datetime.now(UTC).timestamp()))},
        }[method]

    return rpc


def _rpc_for_swap(
    usdc_received: int,
    *,
    eth_value: int = 10**18,
    sender: str = WALLET,
):
    transaction = {
        "hash": TX,
        "from": sender,
        "to": ROUTER,
        "value": hex(eth_value),
    }
    logs = (
        [_transfer_log(USDC, POOL, TREASURY, usdc_received)]
        if usdc_received
        else []
    )
    receipt = {
        "status": "0x1",
        "blockNumber": hex(100),
        "logs": logs,
    }

    async def rpc(method, _params):
        return {
            "eth_chainId": hex(8453),
            "eth_getTransactionByHash": transaction,
            "eth_getTransactionReceipt": receipt,
            "eth_blockNumber": hex(102),
            "eth_getBlockByNumber": {"timestamp": hex(1_785_139_200)},
        }[method]

    return rpc


@pytest.mark.asyncio
async def test_usdc_claim_is_atomic_and_idempotent(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(USDC, 5_000_000))
    account = {"account_id": db, "wallet": WALLET}

    first = await deposits.verify_and_credit(TX, account)
    second = await deposits.verify_and_credit(TX, account)

    assert first["credited"] is True
    assert first["amount_usd"] == 5.0
    assert first["balance_usd"] == 5.0
    assert second["credited"] is False
    assert second["already_claimed"] is True
    assert second["balance_usd"] == 5.0

    async with await database.new_session() as session:
        receipt_count = await session.scalar(sa.select(sa.func.count()).select_from(deposits_t))
        ledger_count = await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger))
    assert receipt_count == 1
    assert ledger_count == 1
    assert await credits.get_balance(db) == 5_000_000


@pytest.mark.asyncio
async def test_credit_failure_rolls_back_deposit_receipt(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(USDC, 5_000_000))

    async def fail_credit(*_args, **_kwargs):
        raise RuntimeError("credit write failed")

    monkeypatch.setattr(credits, "_credit_in_session", fail_credit)
    with pytest.raises(RuntimeError, match="credit write failed"):
        await deposits.verify_and_credit(TX, {"account_id": db, "wallet": WALLET})

    async with await database.new_session() as session:
        receipt_count = await session.scalar(sa.select(sa.func.count()).select_from(deposits_t))
        ledger_count = await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger))
    assert receipt_count == 0
    assert ledger_count == 0


@pytest.mark.asyncio
async def test_usdc_claim_rejects_over_cap_transfer(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(USDC, 100_000_001))

    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 422
    assert await credits.get_balance(db) == 0
    assert await _deposit_count() == 0


@pytest.mark.asyncio
async def test_claim_requires_transaction_from_linked_wallet(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(USDC, 5_000_000, sender=OTHER))
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit(TX, {"account_id": db, "wallet": WALLET})
    assert exc.value.status_code == 403
    assert await credits.get_balance(db) == 0


@pytest.mark.asyncio
async def test_claim_rejects_rpc_on_the_wrong_chain(db, funding, monkeypatch):
    rpc = _rpc_for(USDC, 5_000_000)

    async def wrong_chain(method, params):
        if method == "eth_chainId":
            return hex(1)
        return await rpc(method, params)

    monkeypatch.setattr(deposits, "_rpc", wrong_chain)
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit(TX, {"account_id": db, "wallet": WALLET})
    assert exc.value.status_code == 502
    assert await credits.get_balance(db) == 0


@pytest.mark.asyncio
async def test_aipg_claim_uses_epoch_haircut_and_records_provenance(db, funding, monkeypatch):
    # 10,000 AIPG at $0.002, less 3% = $19.40.
    amount = 10_000 * 10**18
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(AIPG, amount))

    result = await deposits.verify_and_credit_aipg(
        TX,
        {"account_id": db, "wallet": WALLET},
    )

    assert result["credited"] is True
    assert result["asset"] == "AIPG"
    assert result["amount_usd"] == 19.4
    assert result["price_source"] == "operator:test-epoch:haircut-300bps"
    assert await credits.get_balance(db) == 19_400_000


@pytest.mark.asyncio
async def test_aipg_rejects_transfer_outside_price_epoch(db, funding, monkeypatch):
    rpc = _rpc_for(AIPG, 10_000 * 10**18)

    async def old_block(method, params):
        if method == "eth_getBlockByNumber":
            return {
                "timestamp": hex(
                    int((datetime.now(UTC) - timedelta(days=2)).timestamp()),
                ),
            }
        return await rpc(method, params)

    monkeypatch.setattr(deposits, "_rpc", old_block)
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_aipg(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 422
    assert await credits.get_balance(db) == 0
    assert await _deposit_count() == 0


@pytest.mark.asyncio
async def test_aipg_expired_epoch_fails_before_rpc(db, funding, monkeypatch):
    monkeypatch.setattr(
        deposits,
        "AIPG_PRICE_VALID_UNTIL_RAW",
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )

    async def should_not_call(*_args):
        raise AssertionError("RPC must not run for an expired price")

    monkeypatch.setattr(deposits, "_rpc", should_not_call)
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_aipg(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_aipg_per_transaction_cap_is_atomic(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "AIPG_MAX_DEPOSIT_MICRO", 1_000_000)
    monkeypatch.setattr(deposits, "AIPG_ACCOUNT_DAILY_MICRO", 1_000_000)
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(AIPG, 10_000 * 10**18))

    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_aipg(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 422
    assert await credits.get_balance(db) == 0
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(deposits_t)) == 0


@pytest.mark.asyncio
async def test_aipg_account_daily_cap_is_atomic(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "AIPG_ACCOUNT_DAILY_MICRO", 30_000_000)
    monkeypatch.setattr(deposits, "AIPG_NETWORK_DAILY_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(AIPG, 10_000 * 10**18))
    account = {"account_id": db, "wallet": WALLET}

    await deposits.verify_and_credit_aipg(TX, account)
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_aipg("0x" + "cd" * 32, account)
    assert exc.value.status_code == 429
    assert await credits.get_balance(db) == 19_400_000


@pytest.mark.asyncio
async def test_aipg_network_daily_cap_is_atomic(db, funding, monkeypatch):
    other_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(accounts).values(
                id=other_id,
                wallet=OTHER,
                flags={},
                created=datetime.now(UTC),
            ),
        )
        await session.commit()
    monkeypatch.setattr(deposits, "AIPG_ACCOUNT_DAILY_MICRO", 100_000_000)
    monkeypatch.setattr(deposits, "AIPG_NETWORK_DAILY_MICRO", 30_000_000)
    monkeypatch.setattr(deposits, "_rpc", _rpc_for(AIPG, 10_000 * 10**18))
    await deposits.verify_and_credit_aipg(TX, {"account_id": db, "wallet": WALLET})

    monkeypatch.setattr(
        deposits,
        "_rpc",
        _rpc_for(AIPG, 10_000 * 10**18, sender=OTHER),
    )
    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_aipg(
            "0x" + "ef" * 32,
            {"account_id": other_id, "wallet": OTHER},
        )
    assert exc.value.status_code == 503
    assert await credits.get_balance(other_id) == 0


@pytest.mark.asyncio
async def test_converted_eth_credits_actual_usdc_and_records_execution(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "ETH_CONVERSION_MODE", "swap_receipt")
    monkeypatch.setattr(deposits, "_rpc", _rpc_for_swap(25_000_000))
    account = {"account_id": db, "wallet": WALLET}

    first = await deposits.verify_and_credit_converted_eth(TX, account)
    second = await deposits.verify_and_credit_converted_eth(TX, account)

    assert first["credited"] is True
    assert first["asset"] == "ETH"
    assert first["amount"] == "1"
    assert first["amount_usd"] == 25.0
    assert first["price_source"] == "swap:actual-base-usdc-proceeds"
    assert second["already_claimed"] is True
    assert await credits.get_balance(db) == 25_000_000

    async with await database.new_session() as session:
        row = (await session.execute(sa.select(deposits_t))).mappings().one()
    assert int(row["amount_raw"]) == 10**18
    assert row["credited_micro"] == 25_000_000
    assert row["price_micro"] == 25_000_000
    assert row["price_block"] == 100
    assert row["price_timestamp"].replace(tzinfo=UTC) == datetime.fromtimestamp(
        1_785_139_200,
        tz=UTC,
    )


@pytest.mark.asyncio
async def test_converted_eth_rejects_missing_usdc_proceeds(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "ETH_CONVERSION_MODE", "swap_receipt")
    monkeypatch.setattr(deposits, "_rpc", _rpc_for_swap(0))

    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_converted_eth(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 422
    assert await credits.get_balance(db) == 0
    assert await _deposit_count() == 0


@pytest.mark.asyncio
async def test_converted_eth_rejects_over_cap_proceeds(db, funding, monkeypatch):
    monkeypatch.setattr(deposits, "ETH_CONVERSION_MODE", "swap_receipt")
    monkeypatch.setattr(deposits, "_rpc", _rpc_for_swap(100_000_001))

    with pytest.raises(HTTPException) as exc:
        await deposits.verify_and_credit_converted_eth(
            TX,
            {"account_id": db, "wallet": WALLET},
        )
    assert exc.value.status_code == 422
    assert await credits.get_balance(db) == 0
    assert await _deposit_count() == 0


async def _deposit_count() -> int:
    async with await database.new_session() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count()).select_from(deposits_t),
            ),
        )


def test_funding_config_is_explicit_about_credit_terms(funding):
    config = deposits.funding_config({"wallet": WALLET})
    assets = {asset["asset"]: asset for asset in config["assets"]}
    assert config["chain"] == {"id": 8453, "name": "Base"}
    assert config["terms"]["credits_transferable"] is False
    assert config["terms"]["credits_withdrawable"] is False
    assert assets["USDC"]["enabled"] is True
    assert assets["USDC"]["maximum_credit_micro"] == 100_000_000
    assert assets["AIPG"]["enabled"] is True
    assert assets["ETH"]["enabled"] is False
    assert assets["ETH"]["status"] == "conversion_required"


def test_funding_config_exposes_swap_receipt_without_direct_send(funding, monkeypatch):
    monkeypatch.setattr(deposits, "ETH_CONVERSION_MODE", "swap_receipt")
    config = deposits.funding_config({"wallet": WALLET})
    eth = next(asset for asset in config["assets"] if asset["asset"] == "ETH")
    assert eth["enabled"] is False
    assert eth["backend_claim_enabled"] is True
    assert eth["status"] == "conversion_ready"
    assert eth["treasury"] == TREASURY
