# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Base deposits -> non-transferable Grid service credits.

USDC is the launch rail and credits 1:1 in integer micro-USD. AIPG is an
explicitly bounded rail: an operator publishes a conservative, expiring price
epoch and the service enforces per-transaction, per-account/day, and
network/day exposure caps. It never derives credit from manipulable spot price.

Every successful claim atomically commits:

* one immutable ``grid_deposits`` receipt with the raw on-chain value and
  valuation provenance; and
* one idempotent ``grid_credit_ledger`` movement plus its cached balance.

ETH verification remains implemented, but the rail stays unavailable unless an
explicit conversion policy is selected. Production should route ETH through a
swap-to-USDC transaction and claim the actual USDC received rather than leave a
dollar liability backed by volatile treasury inventory.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import credits as credits_t
from ..v2.schema import deposits as deposits_t
from . import alerts, credits

logger = logging.getLogger("grid_api.deposits")

CHAIN_ID = int(os.getenv("GRID_BASE_CHAIN_ID", "8453") or 8453)
DEPOSITS_ENABLED = os.getenv("GRID_DEPOSITS_ENABLED", "0").lower() in ("1", "true", "yes", "on")
TREASURY = os.getenv("GRID_USDC_TREASURY", "").strip().lower()
BASE_RPC = os.getenv("GRID_BASE_RPC", "https://mainnet.base.org").strip()
USDC = os.getenv(
    "GRID_USDC_CONTRACT",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
).strip().lower()
CONFIRMATIONS = max(1, int(os.getenv("GRID_DEPOSIT_CONFIRMATIONS", "3") or 3))
MIN_CREDIT_MICRO = max(1, int(os.getenv("GRID_DEPOSIT_MIN_MICRO", "10000") or 10000))

AIPG_ENABLED = os.getenv("GRID_AIPG_DEPOSITS_ENABLED", "0").lower() in ("1", "true", "yes", "on")
AIPG_TOKEN = os.getenv(
    "GRID_AIPG_TOKEN",
    "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608",
).strip().lower()
AIPG_TREASURY = (os.getenv("GRID_AIPG_TREASURY", "") or TREASURY).strip().lower()
AIPG_DECIMALS = int(os.getenv("GRID_AIPG_DECIMALS", "18") or 18)
AIPG_PRICE_MICRO = int(os.getenv("GRID_AIPG_CREDIT_PRICE_MICRO", "0") or 0)
AIPG_PRICE_EPOCH = os.getenv("GRID_AIPG_PRICE_EPOCH", "").strip()
AIPG_PRICE_AS_OF_RAW = os.getenv("GRID_AIPG_PRICE_AS_OF", "").strip()
AIPG_PRICE_VALID_UNTIL_RAW = os.getenv("GRID_AIPG_PRICE_VALID_UNTIL", "").strip()
AIPG_PRICE_BLOCK = int(os.getenv("GRID_AIPG_PRICE_BLOCK", "0") or 0)
AIPG_PRICE_MAX_AGE_SECONDS = max(
    300,
    int(os.getenv("GRID_AIPG_PRICE_MAX_AGE_SECONDS", "86400") or 86400),
)
AIPG_HAIRCUT_BPS = min(
    5_000,
    max(0, int(os.getenv("GRID_AIPG_DEPOSIT_HAIRCUT_BPS", "300") or 300)),
)
AIPG_MAX_DEPOSIT_MICRO = max(
    MIN_CREDIT_MICRO,
    int(os.getenv("GRID_AIPG_MAX_DEPOSIT_MICRO", "100000000") or 100_000_000),
)
AIPG_ACCOUNT_DAILY_MICRO = max(
    AIPG_MAX_DEPOSIT_MICRO,
    int(os.getenv("GRID_AIPG_ACCOUNT_DAILY_MICRO", "100000000") or 100_000_000),
)
AIPG_NETWORK_DAILY_MICRO = max(
    AIPG_ACCOUNT_DAILY_MICRO,
    int(os.getenv("GRID_AIPG_NETWORK_DAILY_MICRO", "500000000") or 500_000_000),
)

# Direct ETH creates USD liabilities before conversion, so it is disabled by
# default even when the shared deposit switch and treasury are configured.
# "buffered" is an explicit operator opt-in for a tightly capped pilot.
ETH_CONVERSION_MODE = os.getenv("GRID_ETH_CONVERSION_MODE", "disabled").strip().lower()
ETH_TREASURY = (os.getenv("GRID_ETH_TREASURY", "") or TREASURY).strip().lower()
ETH_HAIRCUT_BPS = min(
    5_000,
    max(100, int(os.getenv("GRID_ETH_DEPOSIT_HAIRCUT_BPS", "100") or 100)),
)
ETH_MAX_DEPOSIT_MICRO = max(
    MIN_CREDIT_MICRO,
    int(os.getenv("GRID_ETH_MAX_DEPOSIT_MICRO", "100000000") or 100_000_000),
)
ETH_ACCOUNT_DAILY_MICRO = max(
    ETH_MAX_DEPOSIT_MICRO,
    int(os.getenv("GRID_ETH_ACCOUNT_DAILY_MICRO", "100000000") or 100_000_000),
)
ETH_NETWORK_DAILY_MICRO = max(
    ETH_ACCOUNT_DAILY_MICRO,
    int(os.getenv("GRID_ETH_NETWORK_DAILY_MICRO", "500000000") or 500_000_000),
)

_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _valid_address(value: str) -> bool:
    value = (value or "").lower()
    return value.startswith("0x") and len(value) == 42 and all(c in "0123456789abcdef" for c in value[2:])


def is_configured() -> bool:
    return DEPOSITS_ENABLED and _valid_address(TREASURY) and _valid_address(USDC)


def _aipg_price_epoch(now: datetime | None = None) -> tuple[datetime, datetime] | None:
    now = now or _now()
    as_of = _parse_time(AIPG_PRICE_AS_OF_RAW)
    valid_until = _parse_time(AIPG_PRICE_VALID_UNTIL_RAW)
    if (
        AIPG_PRICE_MICRO <= 0
        or not AIPG_PRICE_EPOCH
        or as_of is None
        or valid_until is None
        or as_of > now
        or valid_until < now
        or valid_until <= as_of
        or now - as_of > timedelta(seconds=AIPG_PRICE_MAX_AGE_SECONDS)
    ):
        return None
    return as_of, valid_until


def aipg_is_configured() -> bool:
    return (
        DEPOSITS_ENABLED
        and AIPG_ENABLED
        and _valid_address(AIPG_TREASURY)
        and _valid_address(AIPG_TOKEN)
        and _aipg_price_epoch() is not None
    )


def eth_is_configured() -> bool:
    return (
        DEPOSITS_ENABLED
        and ETH_CONVERSION_MODE == "buffered"
        and _valid_address(ETH_TREASURY)
    )


def funding_config(account: dict) -> dict:
    """Safe client configuration for the signed-in Console funding flow."""
    epoch = _aipg_price_epoch()
    wallet = (account.get("wallet") or "").lower()
    return {
        "chain": {"id": CHAIN_ID, "name": "Base"},
        "linked_wallet": wallet if _valid_address(wallet) else None,
        "terms": {
            "unit": "USD",
            "credits_transferable": False,
            "credits_withdrawable": False,
            "refund_policy": "operator_review_to_source",
        },
        "assets": [
            {
                "asset": "USDC",
                "enabled": is_configured(),
                "treasury": TREASURY or None,
                "token_address": USDC,
                "decimals": 6,
                "price_micro": 1_000_000,
                "minimum_credit_micro": MIN_CREDIT_MICRO,
                "status": "available" if is_configured() else "disabled",
            },
            {
                "asset": "AIPG",
                "enabled": aipg_is_configured(),
                "treasury": AIPG_TREASURY or None,
                "token_address": AIPG_TOKEN,
                "decimals": AIPG_DECIMALS,
                "price_micro": AIPG_PRICE_MICRO if epoch else None,
                "price_epoch": AIPG_PRICE_EPOCH if epoch else None,
                "price_valid_until": epoch[1].isoformat() if epoch else None,
                "haircut_bps": AIPG_HAIRCUT_BPS,
                "minimum_credit_micro": MIN_CREDIT_MICRO,
                "maximum_credit_micro": AIPG_MAX_DEPOSIT_MICRO,
                "account_daily_micro": AIPG_ACCOUNT_DAILY_MICRO,
                "status": "available" if aipg_is_configured() else "price_unavailable",
            },
            {
                "asset": "ETH",
                # A buffered treasury pilot can accept operator-reviewed claims,
                # but the public Console must wait for conversion-backed funding.
                "enabled": False,
                "backend_claim_enabled": eth_is_configured(),
                "treasury": ETH_TREASURY or None,
                "token_address": None,
                "decimals": 18,
                "conversion_mode": ETH_CONVERSION_MODE,
                "haircut_bps": ETH_HAIRCUT_BPS,
                "minimum_credit_micro": MIN_CREDIT_MICRO,
                "maximum_credit_micro": ETH_MAX_DEPOSIT_MICRO,
                "status": "operator_pilot" if eth_is_configured() else "conversion_required",
            },
        ],
    }


async def _rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"rpc {method}: {body['error']}")
        return body.get("result")


def _normalize_tx_hash(tx_hash: str) -> str:
    value = (tx_hash or "").strip().lower()
    if not (value.startswith("0x") and len(value) == 66 and all(c in "0123456789abcdef" for c in value[2:])):
        raise HTTPException(400, detail="tx_hash must be a 0x-prefixed 32-byte hash.")
    return value


def _addr_from_topic(topic: str) -> str:
    return ("0x" + topic[-40:]).lower()


async def _confirmed_transaction(tx_hash: str, asset: str) -> tuple[dict, dict, int]:
    try:
        chain_id = int(await _rpc("eth_chainId", []), 16)
        if chain_id != CHAIN_ID:
            raise RuntimeError(
                f"configured Base RPC returned chain id {chain_id}, expected {CHAIN_ID}",
            )
        tx = await _rpc("eth_getTransactionByHash", [tx_hash])
        receipt = await _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as exc:
        logger.warning("%s deposit rpc failed for %s: %s", asset.lower(), tx_hash, exc)
        alerts.emit(
            "deposit_rpc_failed",
            "critical",
            "A Base deposit claim could not be verified.",
            fields={"asset": asset, "tx": alerts.opaque_id(tx_hash), "error_type": type(exc).__name__},
            dedupe_key=f"deposit-rpc:{asset.lower()}",
        )
        raise HTTPException(502, detail="Could not reach Base to verify the transaction.")
    if not tx or not receipt:
        raise HTTPException(400, detail="Transaction not found or not yet mined.")
    if receipt.get("status") not in ("0x1", 1):
        raise HTTPException(400, detail="Transaction failed on-chain.")
    block_number = int(receipt["blockNumber"], 16)
    try:
        latest_raw = await _rpc("eth_blockNumber", [])
        confirmations = int(latest_raw, 16) - block_number + 1
    except Exception:
        confirmations = 0
    if confirmations < CONFIRMATIONS:
        raise HTTPException(
            425,
            detail=f"Only {max(0, confirmations)} confirmations; need {CONFIRMATIONS}. Retry shortly.",
        )
    return tx, receipt, block_number


def _linked_sender(tx: dict, account: dict, asset: str) -> str:
    sender = (tx.get("from") or "").lower()
    wallet = (account.get("wallet") or "").lower()
    if not _valid_address(wallet):
        raise HTTPException(403, detail="Link a wallet before claiming Base deposits.")
    if sender != wallet:
        alerts.emit(
            "deposit_wallet_mismatch",
            "warning",
            "A deposit claim sender did not match the authenticated wallet.",
            fields={
                "asset": asset,
                "account": alerts.opaque_id(account.get("account_id")),
                "tx": alerts.opaque_id(tx.get("hash")),
            },
            dedupe_key=f"deposit-wallet-mismatch:{alerts.opaque_id(account.get('account_id'))}",
        )
        raise HTTPException(403, detail="This deposit was sent from a different wallet than your account's.")
    return sender


def _direct_erc20_amount(receipt: dict, token: str, treasury: str, sender: str) -> int:
    """Sum direct wallet->treasury transfers of one token in the transaction."""
    amount = 0
    for event in receipt.get("logs", []):
        topics = event.get("topics", [])
        if (
            (event.get("address") or "").lower() == token
            and len(topics) >= 3
            and topics[0].lower() == _TRANSFER_TOPIC
            and _addr_from_topic(topics[1]) == sender
            and _addr_from_topic(topics[2]) == treasury
        ):
            amount += int(event.get("data", "0x0"), 16)
    return amount


async def _lock_network_cap(session, asset: str) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
            {"name": f"grid:{asset.lower()}-deposit-cap"},
        )


async def _enforce_daily_caps(
    session,
    *,
    account_id,
    asset: str,
    credit_micro: int,
    per_tx_micro: int,
    account_daily_micro: int,
    network_daily_micro: int,
) -> None:
    if credit_micro > per_tx_micro:
        raise HTTPException(
            422,
            detail=f"{asset} deposit exceeds the ${per_tx_micro / 1_000_000:.2f} pilot maximum.",
        )
    await _lock_network_cap(session, asset)
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    sums = (
        await session.execute(
            sa.select(
                sa.func.coalesce(
                    sa.func.sum(
                        sa.case(
                            (deposits_t.c.account_id == account_id, deposits_t.c.credited_micro),
                            else_=0,
                        ),
                    ),
                    0,
                ),
                sa.func.coalesce(sa.func.sum(deposits_t.c.credited_micro), 0),
            ).where(
                sa.and_(
                    deposits_t.c.asset == asset,
                    deposits_t.c.status == "credited",
                    deposits_t.c.created >= start,
                ),
            ),
        )
    ).one()
    account_used, network_used = int(sums[0]), int(sums[1])
    if account_used + credit_micro > account_daily_micro:
        raise HTTPException(429, detail=f"{asset} account funding limit reached for today.")
    if network_used + credit_micro > network_daily_micro:
        raise HTTPException(503, detail=f"{asset} network funding limit reached for today.")


async def _existing_deposit(chain_id: int, asset: str, tx_hash: str) -> dict | None:
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(deposits_t).where(
                    sa.and_(
                        deposits_t.c.chain_id == chain_id,
                        deposits_t.c.asset == asset,
                        deposits_t.c.tx_hash == tx_hash,
                    ),
                ),
            )
        ).mappings().first()
    return dict(row) if row else None


async def _record_and_credit(
    *,
    account: dict,
    asset: str,
    token_address: str | None,
    tx_hash: str,
    block_number: int,
    sender: str,
    treasury: str,
    amount_raw: int,
    decimals: int,
    price_micro: int,
    price_source: str,
    price_timestamp: datetime,
    price_block: int | None,
    credited_micro: int,
    caps: tuple[int, int, int] | None = None,
) -> tuple[bool, dict, int]:
    ref = f"base:{CHAIN_ID}:{asset.lower()}:{tx_hash}"
    async with await new_session() as session:
        canonical_id = await credits._locked_canonical_account(session, account["account_id"])
        existing = (
            await session.execute(
                sa.select(deposits_t).where(
                    sa.and_(
                        deposits_t.c.chain_id == CHAIN_ID,
                        deposits_t.c.asset == asset,
                        deposits_t.c.tx_hash == tx_hash,
                    ),
                ),
            )
        ).mappings().first()
        if existing:
            if existing["account_id"] != canonical_id:
                raise HTTPException(409, detail="This Base transaction was already claimed.")
            balance = (
                await session.execute(
                    sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == canonical_id),
                )
            ).scalar_one_or_none() or 0
            return False, dict(existing), int(balance)
        if caps:
            await _enforce_daily_caps(
                session,
                account_id=canonical_id,
                asset=asset,
                credit_micro=credited_micro,
                per_tx_micro=caps[0],
                account_daily_micro=caps[1],
                network_daily_micro=caps[2],
            )
        values = {
            "account_id": canonical_id,
            "chain_id": CHAIN_ID,
            "asset": asset,
            "token_address": token_address,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "from_address": sender,
            "treasury_address": treasury,
            "amount_raw": Decimal(amount_raw),
            "amount_decimals": decimals,
            "price_micro": price_micro,
            "price_source": price_source,
            "price_timestamp": price_timestamp,
            "price_block": price_block,
            "credited_micro": credited_micro,
            "refund_address": sender,
            "status": "credited",
            "created": _now(),
        }
        try:
            inserted = (
                await session.execute(sa.insert(deposits_t).values(**values).returning(deposits_t))
            ).mappings().one()
            await credits._credit_in_session(
                session,
                canonical_id,
                credited_micro,
                reason=f"{asset.lower()}_deposit",
                ref=ref,
            )
            balance = (
                await session.execute(
                    sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == canonical_id),
                )
            ).scalar_one()
            await session.commit()
            return True, dict(inserted), int(balance)
        except IntegrityError:
            await session.rollback()

    existing = await _existing_deposit(CHAIN_ID, asset, tx_hash)
    if not existing:
        logger.error("deposit idempotency conflict without receipt asset=%s tx=%s", asset, tx_hash)
        raise HTTPException(409, detail="Deposit claim conflicted with an existing credit reference.")
    async with await new_session() as session:
        canonical_id = await credits._locked_canonical_account(session, account["account_id"])
    if str(existing["account_id"]) != str(canonical_id):
        raise HTTPException(409, detail="This Base transaction was already claimed.")
    balance = await credits.get_balance(existing["account_id"])
    return False, existing, balance


def _response(applied: bool, deposit: dict, balance: int) -> dict:
    raw = int(deposit["amount_raw"])
    decimals = int(deposit["amount_decimals"])
    return {
        "credited": applied,
        "already_claimed": not applied,
        "deposit_id": int(deposit["id"]),
        "asset": deposit["asset"],
        "amount": format(Decimal(raw) / (Decimal(10) ** decimals), "f"),
        "amount_raw": str(raw),
        "amount_usd": round(int(deposit["credited_micro"]) / 1_000_000, 6),
        "balance_usd": round(balance / 1_000_000, 6),
        "from": deposit["from_address"],
        "tx_hash": deposit["tx_hash"],
        "block_number": int(deposit["block_number"]),
        "price_source": deposit["price_source"],
    }


async def verify_and_credit(tx_hash: str, account: dict) -> dict:
    """Verify and atomically credit a direct USDC transfer on Base."""
    if not is_configured():
        raise HTTPException(503, detail="USDC deposits are not enabled on this grid yet.")
    tx_hash = _normalize_tx_hash(tx_hash)
    tx, receipt, block_number = await _confirmed_transaction(tx_hash, "USDC")
    sender = _linked_sender(tx, account, "USDC")
    amount_raw = _direct_erc20_amount(receipt, USDC, TREASURY, sender)
    if amount_raw <= 0:
        raise HTTPException(400, detail="No direct USDC transfer to the grid treasury was found.")
    if amount_raw < MIN_CREDIT_MICRO:
        raise HTTPException(422, detail="USDC deposit is below the minimum funding amount.")
    applied, deposit, balance = await _record_and_credit(
        account=account,
        asset="USDC",
        token_address=USDC,
        tx_hash=tx_hash,
        block_number=block_number,
        sender=sender,
        treasury=TREASURY,
        amount_raw=amount_raw,
        decimals=6,
        price_micro=1_000_000,
        price_source="usdc:1:1",
        price_timestamp=_now(),
        price_block=block_number,
        credited_micro=amount_raw,
    )
    if applied:
        alerts.emit(
            "deposit_credited",
            "success",
            "A verified Base deposit was credited to a Grid account.",
            fields={
                "asset": "USDC",
                "account": alerts.opaque_id(account["account_id"]),
                "tx": alerts.opaque_id(tx_hash),
                "amount_micro": amount_raw,
            },
            dedupe_key=f"deposit-credited:usdc:{alerts.opaque_id(tx_hash)}",
        )
    return _response(applied, deposit, balance)


async def verify_and_credit_aipg(tx_hash: str, account: dict) -> dict:
    """Credit a direct AIPG transfer under a bounded, expiring price epoch."""
    epoch = _aipg_price_epoch()
    if not aipg_is_configured() or epoch is None:
        raise HTTPException(503, detail="AIPG deposits do not have a valid funding price right now.")
    tx_hash = _normalize_tx_hash(tx_hash)
    tx, receipt, block_number = await _confirmed_transaction(tx_hash, "AIPG")
    sender = _linked_sender(tx, account, "AIPG")
    amount_raw = _direct_erc20_amount(receipt, AIPG_TOKEN, AIPG_TREASURY, sender)
    if amount_raw <= 0:
        raise HTTPException(400, detail="No direct AIPG transfer to the grid treasury was found.")
    market_micro = amount_raw * AIPG_PRICE_MICRO // (10 ** AIPG_DECIMALS)
    credited_micro = market_micro * (10_000 - AIPG_HAIRCUT_BPS) // 10_000
    if credited_micro < MIN_CREDIT_MICRO:
        raise HTTPException(422, detail="AIPG deposit is below the minimum funding amount.")
    source = f"operator:{AIPG_PRICE_EPOCH}:haircut-{AIPG_HAIRCUT_BPS}bps"
    applied, deposit, balance = await _record_and_credit(
        account=account,
        asset="AIPG",
        token_address=AIPG_TOKEN,
        tx_hash=tx_hash,
        block_number=block_number,
        sender=sender,
        treasury=AIPG_TREASURY,
        amount_raw=amount_raw,
        decimals=AIPG_DECIMALS,
        price_micro=AIPG_PRICE_MICRO,
        price_source=source,
        price_timestamp=epoch[0],
        price_block=AIPG_PRICE_BLOCK or None,
        credited_micro=credited_micro,
        caps=(AIPG_MAX_DEPOSIT_MICRO, AIPG_ACCOUNT_DAILY_MICRO, AIPG_NETWORK_DAILY_MICRO),
    )
    if applied:
        alerts.emit(
            "deposit_credited",
            "success",
            "A verified Base deposit was credited to a Grid account.",
            fields={
                "asset": "AIPG",
                "account": alerts.opaque_id(account["account_id"]),
                "tx": alerts.opaque_id(tx_hash),
                "amount_micro": credited_micro,
                "price_epoch": AIPG_PRICE_EPOCH,
            },
            dedupe_key=f"deposit-credited:aipg:{alerts.opaque_id(tx_hash)}",
        )
    return _response(applied, deposit, balance)


async def verify_and_credit_eth(tx_hash: str, account: dict) -> dict:
    """Verify a tightly capped native-ETH pilot deposit.

    The default conversion mode is ``disabled``. ``buffered`` is an explicit
    operator opt-in and applies a valuation haircut plus daily exposure caps;
    it is not a substitute for the target swap-to-USDC rail.
    """
    if not eth_is_configured():
        raise HTTPException(
            503,
            detail="Direct ETH funding is unavailable until a conversion-backed rail is configured.",
        )
    tx_hash = _normalize_tx_hash(tx_hash)
    tx, receipt, block_number = await _confirmed_transaction(tx_hash, "ETH")
    sender = _linked_sender(tx, account, "ETH")
    if (tx.get("to") or "").lower() != ETH_TREASURY:
        raise HTTPException(400, detail="This transaction did not send ETH to the grid treasury.")
    amount_raw = int(tx.get("value", "0x0") or "0x0", 16)
    if amount_raw <= 0:
        raise HTTPException(400, detail="No ETH value in this transaction.")
    from . import holdings

    try:
        market_price_micro = await holdings.eth_usd_micro()
    except Exception as exc:
        logger.warning("eth/usd price read failed for deposit %s: %s", tx_hash, exc)
        alerts.emit(
            "deposit_oracle_failed",
            "critical",
            "An ETH deposit could not be priced from the Base Chainlink feed.",
            fields={"asset": "ETH", "tx": alerts.opaque_id(tx_hash), "error_type": type(exc).__name__},
            dedupe_key="deposit-oracle:eth-usd",
        )
        raise HTTPException(502, detail="Could not read the ETH/USD price feed; retry shortly.")
    price_micro = market_price_micro * (10_000 - ETH_HAIRCUT_BPS) // 10_000
    credited_micro = amount_raw * price_micro // (10 ** 18)
    if credited_micro < MIN_CREDIT_MICRO:
        raise HTTPException(422, detail="ETH deposit is below the minimum funding amount.")
    applied, deposit, balance = await _record_and_credit(
        account=account,
        asset="ETH",
        token_address=None,
        tx_hash=tx_hash,
        block_number=block_number,
        sender=sender,
        treasury=ETH_TREASURY,
        amount_raw=amount_raw,
        decimals=18,
        price_micro=price_micro,
        price_source=f"chainlink:eth-usd:haircut-{ETH_HAIRCUT_BPS}bps",
        price_timestamp=_now(),
        price_block=block_number,
        credited_micro=credited_micro,
        caps=(ETH_MAX_DEPOSIT_MICRO, ETH_ACCOUNT_DAILY_MICRO, ETH_NETWORK_DAILY_MICRO),
    )
    if applied:
        alerts.emit(
            "deposit_credited",
            "success",
            "A verified Base deposit was credited to a Grid account.",
            fields={
                "asset": "ETH",
                "account": alerts.opaque_id(account["account_id"]),
                "tx": alerts.opaque_id(tx_hash),
                "amount_micro": credited_micro,
            },
            dedupe_key=f"deposit-credited:eth:{alerts.opaque_id(tx_hash)}",
        )
    return _response(applied, deposit, balance)


async def list_deposits(account: dict, limit: int = 50) -> list[dict]:
    """Return the signed-in account's immutable funding receipts."""
    limit = max(1, min(int(limit or 50), 100))
    async with await new_session() as session:
        canonical_id = await credits._locked_canonical_account(session, account["account_id"])
        rows = (
            await session.execute(
                sa.select(deposits_t)
                .where(deposits_t.c.account_id == canonical_id)
                .order_by(deposits_t.c.created.desc())
                .limit(limit),
            )
        ).mappings().all()
    return [
        {
            "deposit_id": int(row["id"]),
            "asset": row["asset"],
            "amount": format(
                Decimal(int(row["amount_raw"])) / (Decimal(10) ** int(row["amount_decimals"])),
                "f",
            ),
            "credited_usd": round(int(row["credited_micro"]) / 1_000_000, 6),
            "tx_hash": row["tx_hash"],
            "block_number": int(row["block_number"]),
            "status": row["status"],
            "price_source": row["price_source"],
            "created": row["created"].isoformat() if row["created"] else None,
        }
        for row in rows
    ]
