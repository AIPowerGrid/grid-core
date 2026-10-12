# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One confirmed asset/transaction funds at most one Grid economic reference.

No RPC or payment sender lives here. Callers must independently prove a receipt
before claiming it in the same transaction as their economic write. All deposit,
text/video x402 writers must use this code; mixed-version writers are unsafe.
Historical books are read as exclusion evidence, never backfilled into claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

import sqlalchemy as sa
from eth_utils import keccak
from sqlalchemy.exc import IntegrityError

from ..v2.schema import deposits, video_payment_attempts, video_quotes, x402_payments
from ..v2.schema import payment_receipt_claims as claims

_AUTH_USED = "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()

class ReceiptClaimError(ValueError):
    """Opaque claim failure; no receipt or database values in the message."""


class ReceiptClaimConflict(ReceiptClaimError):
    """Another economic reference owns or may already have consumed this value."""


def _json(value) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        if len(encoded) > 8192 or type(value) is not dict:
            raise ValueError
        return encoded
    except (TypeError, ValueError, RecursionError):
        raise ReceiptClaimError("invalid bounded receipt purpose") from None


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _identity(chain_id: int, asset: str, transaction_hash: str) -> tuple[str, str, str]:
    if type(chain_id) is not int or not 0 < chain_id <= 2**31 - 1:
        raise ReceiptClaimError("valid receipt chain required")
    if type(asset) is not str or (asset != "native" and not re.fullmatch(r"0x[0-9a-fA-F]{40}", asset)):
        raise ReceiptClaimError("valid receipt asset required")
    asset = asset.lower()
    if asset != "native" and int(asset[2:], 16) == 0:
        raise ReceiptClaimError("nonzero receipt token required")
    if type(transaction_hash) is not str or not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
        raise ReceiptClaimError("valid receipt transaction required")
    tx = transaction_hash.lower()
    return _digest({"schema": "aipg.receipt-identity.v1", "chain_id": chain_id, "asset": asset, "transaction_hash": tx}), asset, tx


def _owner(kind: str, ref: str) -> None:
    if kind not in {"deposit", "text_x402", "video_x402"} or type(ref) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", ref):
        raise ReceiptClaimError("valid economic receipt owner required")


def _validate(row: dict) -> None:
    key, asset, tx = _identity(row["chain_id"], row["asset"], row["transaction_hash"])
    _owner(row["owner_kind"], row["owner_ref"])
    if row["claim_key"] != key or asset != row["asset"] or tx != row["transaction_hash"] or row["purpose_hash"] != _digest(row["purpose"]):
        raise ReceiptClaimError("stored receipt claim changed")


def observed_authorizations(receipt: dict, asset: str) -> tuple[tuple[str, str], ...]:
    """Read public spent-nonce witnesses, never signatures or funding authority."""
    if type(receipt) is not dict or type(asset) is not str:
        raise ReceiptClaimError("structured receipt authorization evidence required")
    events = receipt.get("logs", [])
    if type(events) is not list or len(events) > 128:
        raise ReceiptClaimError("receipt authorization bound exceeded")
    result = set()
    for event in events:
        if type(event) is not dict or type(event.get("topics", [])) is not list:
            raise ReceiptClaimError("structured authorization event required")
        topics = event.get("topics", [])
        if type(event.get("address", "")) is not str or any(type(topic) is not str for topic in topics):
            raise ReceiptClaimError("structured authorization event required")
        if ((event.get("address") or "").lower() != asset.lower() or not topics
                or topics[0].lower() != _AUTH_USED):
            continue
        if (len(topics) != 3 or any(type(topic) is not str or not re.fullmatch(r"0x[0-9a-fA-F]{64}", topic) for topic in topics)
                or topics[1][2:26] != "0" * 24 or int(topics[1][26:], 16) == 0
                or event.get("removed", False) is not False or event.get("data") != "0x"):
            raise ReceiptClaimError("invalid spent authorization witness")
        result.add(("0x" + topics[1][-40:].lower(), topics[2].lower()))
    return tuple(sorted(result))


async def _reserved_authorizations(session, *, chain_id: int, asset: str, witnesses: tuple,
                                   owner_kind: str, owner_ref: str) -> None:
    if type(witnesses) is not tuple or len(witnesses) > 128:
        raise ReceiptClaimError("bounded spent authorization witnesses required")
    for witness in witnesses:
        if type(witness) is not tuple or len(witness) != 2:
            raise ReceiptClaimError("canonical spent authorization witness required")
        payer, nonce = witness
        if (type(payer) is not str or not re.fullmatch(r"0x[0-9a-f]{40}", payer)
                or type(nonce) is not str or not re.fullmatch(r"0x[0-9a-f]{64}", nonce)):
            raise ReceiptClaimError("canonical spent authorization witness required")
        rows = (await session.execute(sa.select(video_payment_attempts.c.quote_id, video_quotes.c.snapshot)
            .join(video_quotes, video_quotes.c.id == video_payment_attempts.c.quote_id)
            .where(video_payment_attempts.c.payer == payer, video_quotes.c.authorization_nonce == nonce).limit(2))).mappings().all()
        for row in rows:
            payment = row["snapshot"]["payment"]
            if (payment["network"] == f"eip155:{chain_id}" and payment["asset"] == asset
                    and (owner_kind != "video_x402" or owner_ref != str(row["quote_id"]))):
                raise ReceiptClaimConflict("spent authorization belongs to another video attempt")


async def _legacy_exclusion(session, *, chain_id: int, asset: str, tx: str, owner_kind: str, owner_ref: str) -> None:
    prior = (await session.execute(sa.select(deposits).where(deposits.c.chain_id == chain_id,
        sa.func.lower(deposits.c.tx_hash) == tx).limit(4))).mappings().all()
    if len(prior) > 3:
        raise ReceiptClaimConflict("ambiguous historical receipt ownership")
    for deposit in prior:
        ref = f"base:{chain_id}:{str(deposit['asset']).lower()}:{tx}"
        token = (deposit["token_address"] or "").lower()
        converted = deposit["asset"] == "ETH" and deposit["price_source"] == "swap:actual-base-usdc-proceeds"
        if converted:
            known = (await session.execute(sa.select(claims).where(
                claims.c.owner_kind == "deposit", claims.c.owner_ref == ref))).mappings().first()
            if known is not None:
                _validate(dict(known))
                matches = known["asset"] == asset
            else:
                # Old conversion books cannot invent a missing USDC token address.
                matches = asset != "native"
        else:
            matches = asset == (token or "native")
        if matches and (owner_kind != "deposit" or owner_ref != ref):
            raise ReceiptClaimConflict("receipt belongs to another historical obligation")
    text = (await session.execute(sa.select(x402_payments.c.job_id).where(
        x402_payments.c.network == f"eip155:{chain_id}", sa.func.lower(x402_payments.c.asset) == asset,
        sa.func.lower(x402_payments.c.tx_hash) == tx).limit(2))).scalars().all()
    if len(text) > 1 or any(owner_kind != "text_x402" or owner_ref != job for job in text):
        raise ReceiptClaimConflict("receipt belongs to another text payment")
    videos = (await session.execute(sa.select(video_payment_attempts.c.quote_id, video_quotes.c.snapshot)
        .join(video_quotes, video_quotes.c.id == video_payment_attempts.c.quote_id)
        .where(sa.func.lower(video_payment_attempts.c.transaction_hash) == tx, video_payment_attempts.c.proof.is_not(None))
        .limit(2))).mappings().all()
    for video in videos:
        payment = video["snapshot"]["payment"]
        if (payment["network"] == f"eip155:{chain_id}" and payment["asset"] == asset
                and (owner_kind != "video_x402" or owner_ref != str(video["quote_id"]))):
            raise ReceiptClaimConflict("receipt belongs to another video payment")


async def claim_in_session(session, *, chain_id: int, asset: str, transaction_hash: str,
                           owner_kind: str, owner_ref: str, purpose: dict, observed_nonces: tuple = ()) -> bool:
    """Caller-owned atomic claim. A conflict leaves its SQL transaction usable.

    One token/transaction is the retained unit: old deposits sum transfers and
    text/video x402 permit one job per transaction. Do not silently split a batch
    into new economic references. Historical rows receive no fabricated claims.
    """
    key, asset, tx = _identity(chain_id, asset, transaction_hash)
    _owner(owner_kind, owner_ref)
    purpose_hash = _digest(purpose)
    if session.bind.dialect.name == "sqlite":
        # Legacy sqlite transaction mode does not BEGIN for SELECT/SAVEPOINT.
        connection = await session.connection()
        physical_transaction = await connection.run_sync(lambda sync: sync.connection.driver_connection.in_transaction)
        if not physical_transaction:
            await session.execute(sa.text("BEGIN"))
    if session.bind.dialect.name == "postgresql":
        lock = int.from_bytes(hashlib.sha256(f"aipg-receipt-lock-v1:{key}".encode("ascii")).digest()[:8], "big", signed=True)
        await session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    current = (await session.execute(sa.select(claims).where(claims.c.claim_key == key))).mappings().one_or_none()
    if current is not None:
        _validate(dict(current))
        if (current["owner_kind"] != owner_kind or current["owner_ref"] != owner_ref or current["purpose_hash"] != purpose_hash):
            raise ReceiptClaimConflict("receipt is already bound to another economic purpose")
        return False
    own = (await session.execute(sa.select(claims.c.claim_key).where(claims.c.owner_kind == owner_kind,
        claims.c.owner_ref == owner_ref))).scalar_one_or_none()
    if own is not None:
        raise ReceiptClaimConflict("economic purpose already has another receipt")
    await _reserved_authorizations(session, chain_id=chain_id, asset=asset, witnesses=observed_nonces,
                                   owner_kind=owner_kind, owner_ref=owner_ref)
    await _legacy_exclusion(session, chain_id=chain_id, asset=asset, tx=tx, owner_kind=owner_kind, owner_ref=owner_ref)
    values = {"claim_key": key, "chain_id": chain_id, "asset": asset, "transaction_hash": tx,
        "owner_kind": owner_kind, "owner_ref": owner_ref, "purpose": json.loads(_json(purpose)),
        "purpose_hash": purpose_hash, "created": datetime.now(UTC)}
    try:
        async with session.begin_nested():
            await session.execute(claims.insert().values(**values))
    except IntegrityError:
        raise ReceiptClaimConflict("receipt ownership conflicted during commit") from None
    return True


async def require_owned_in_session(session, *, chain_id: int, asset: str, transaction_hash: str,
                                   owner_kind: str, owner_ref: str) -> dict:
    """Read-only ownership gate for future atomic funded admission, not a claim."""
    key, _asset, _tx = _identity(chain_id, asset, transaction_hash)
    _owner(owner_kind, owner_ref)
    row = (await session.execute(sa.select(claims).where(claims.c.claim_key == key))).mappings().one_or_none()
    if row is None or row["owner_kind"] != owner_kind or row["owner_ref"] != owner_ref:
        raise ReceiptClaimConflict("matching receipt ownership is unavailable")
    result = dict(row)
    _validate(result)
    return result
