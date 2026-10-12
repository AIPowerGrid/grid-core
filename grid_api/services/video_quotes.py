# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable private quotes, without payment or executable job authority.

Expired identities remain recoverable and consume the pilot's retained-row caps.
There is intentionally no deletion, authorization acceptance or sender here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import accounts, video_quotes
from . import identities, pricing, video_plans

TTL_SECONDS = 300
MAX_SNAPSHOT_BYTES = 512 * 1024
_LOCK = int.from_bytes(hashlib.sha256(b"aipg-video-quotes-capacity-v1").digest()[:8], "big", signed=True)
_HASH = re.compile(r"^[0-9a-f]{64}$")
# Circle's canonical contracts, not an operator-selected bridged asset.
USDC = {
    "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
}
_preparing: set[UUID] = set()


def payment_domain(network: str) -> dict:
    """Pinned canonical USDC domains; live contract verification is separate."""
    if network not in USDC:
        raise VideoQuoteError("unsupported video payment network")
    return {"name": "USD Coin" if network == "eip155:8453" else "USDC", "version": "2",
            "chainId": int(network.split(":")[1]), "verifyingContract": USDC[network]}


class VideoQuoteError(ValueError):
    """Invalid quote contract; messages contain no request values."""


class VideoQuoteConflict(VideoQuoteError):
    """Reused or ambiguous immutable identity."""


class VideoQuoteCapacity(VideoQuoteError):
    """Retained-row or process-local preparation budget exhausted."""


class VideoQuoteUnpriced(VideoQuoteError):
    """No positive governed price within the client/operator ceiling."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _uuid(value) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise VideoQuoteError("valid UUID required") from exc


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise VideoQuoteError("lowercase SHA-256 required")
    return value


def _canonical(value) -> str:
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise VideoQuoteError("invalid quote snapshot") from exc
    if len(result) > MAX_SNAPSHOT_BYTES:
        raise VideoQuoteError("quote snapshot exceeds its bound")
    return result


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _key_hash(value: str) -> str:
    key = _uuid(value)
    if key.version != 4:
        raise VideoQuoteError("idempotency key must be a UUIDv4")
    return hashlib.sha256(f"aipg-video-quote-key-v1:{key}".encode("ascii")).hexdigest()


def validate_idempotency_key(value: str) -> None:
    _key_hash(value)


def _address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value[2:], 16) == 0:
        raise VideoQuoteError("nonzero EVM recipient required")
    return value.lower()


@contextmanager
def preparation_slot(account_id):
    """No queued decoder work: at most two preparing owners per process.

    Runs on the request event loop; no await occurs between checking and taking
    a slot. MP4 decoding additionally retains its own slot after cancellation.
    This is a local resource bound, not a cross-replica SQL capacity proof.
    """
    owner = _uuid(account_id)
    if owner in _preparing or len(_preparing) >= 2:
        raise VideoQuoteCapacity("video quote preparation is busy")
    _preparing.add(owner)
    try:
        yield
    finally:
        _preparing.remove(owner)


@dataclass(frozen=True)
class PreparedVideoQuote:
    snapshot_json: str = field(repr=False)

    @property
    def snapshot(self) -> dict:
        return json.loads(self.snapshot_json)

    @property
    def values(self) -> dict:
        snapshot = self.snapshot
        return {
            "id": _uuid(snapshot["quote_id"]), "job_id": _uuid(snapshot["job_id"]),
            "account_id": _uuid(snapshot["owner"]), "idempotency_hash": snapshot["idempotency_hash"],
            "request_hash": snapshot["request_hash"], "plan_hash": snapshot["plan_hash"],
            "commitment": _hash(snapshot),
            "authorization_nonce": "0x" + _hash({"schema": "aipg.video-authorization.v1", "quote": _hash(snapshot)}),
            "snapshot": snapshot, "amount_micro": snapshot["payment"]["amount_micro"],
            "max_cost_micro": snapshot["max_cost_micro"],
            "created": datetime.fromtimestamp(snapshot["created"], UTC),
            "expires": datetime.fromtimestamp(snapshot["expires"], UTC),
        }


def prepare(*, account_id, idempotency_key: str, request_hash: str, plan: video_plans.FrozenVideoPlan,
            max_cost_micro: int, network: str, pay_to: str, server_max_micro: int,
            preferred_worker: str = "") -> PreparedVideoQuote:
    """Freeze validated execution and the undiscounted exact-USDC tariff once."""
    owner, key_hash, intent = _uuid(account_id), _key_hash(idempotency_key), _digest(request_hash)
    if network not in USDC:
        raise VideoQuoteError("unsupported video payment network")
    if type(preferred_worker) is not str or len(preferred_worker) > 255:
        raise VideoQuoteError("bounded worker affinity required")
    pay_to = _address(pay_to)
    if any(type(cap) is not int or not 0 < cap <= 2**63 - 1 for cap in (max_cost_micro, server_max_micro)):
        raise VideoQuoteError("positive bounded cost ceilings required")
    if not isinstance(plan, video_plans.FrozenVideoPlan):
        raise VideoQuoteError("compiled video plan required")
    plan.check_inputs()
    frozen = plan.snapshot
    seconds = frozen["payload"].get("seconds")
    price = pricing.get_price(plan.model)
    rate = price.video_per_second if price else 0
    if (type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0
            or type(rate) not in {int, float} or not math.isfinite(rate) or rate <= 0):
        raise VideoQuoteUnpriced("positive governed video tariff required")
    amount = math.ceil(rate * seconds * pricing.MICRO)
    if not 0 < amount <= min(max_cost_micro, server_max_micro):
        raise VideoQuoteUnpriced("video tariff exceeds the cost ceiling")
    created = _now().replace(microsecond=0)
    expires = created + timedelta(seconds=TTL_SECONDS)
    quote = PreparedVideoQuote(_canonical({
        "schema": "aipg.video-quote-binding.v2", "quote_id": str(uuid4()), "job_id": str(uuid4()),
        "owner": str(owner), "idempotency_hash": key_hash, "request_hash": intent,
        "preferred_worker": preferred_worker,
        "plan": frozen, "plan_hash": plan.sha256,
        "price": {"version": pricing.PRICE_BOOK_VERSION, "currency": "USD", "video_per_second_usd": rate,
                  "discount_bps": 0},
        "payment": {"scheme": "exact", "network": network, "asset": USDC[network], "pay_to": pay_to,
                    "amount_micro": amount, "valid_after": 0, "valid_before": int(expires.timestamp()),
                    "eip712_domain": payment_domain(network)},
        "max_cost_micro": max_cost_micro, "created": int(created.timestamp()), "expires": int(expires.timestamp()),
        "salt": secrets.token_hex(32),
    }))
    _validate_row(quote.values)
    return quote


def _validate_row(row: dict) -> dict:
    """Prove the stored commitment and its indexed fields before every replay."""
    try:
        snapshot = row["snapshot"]
        expected = PreparedVideoQuote(_canonical(snapshot)).values
        if any(row[key] != value for key, value in expected.items() if key not in {"created", "expires"}):
            raise VideoQuoteError("video quote binding changed")
        for key in ("created", "expires"):
            if _aware(row[key]) != expected[key]:
                raise VideoQuoteError("video quote deadline changed")
        payment, plan, price = snapshot["payment"], snapshot["plan"], snapshot["price"]
        rate, seconds = price["video_per_second_usd"], plan["payload"]["seconds"]
        if (snapshot["schema"] not in {"aipg.video-quote-binding.v1", "aipg.video-quote-binding.v2"}
                or plan["schema"] != "aipg.video-plan.v1"
                or _hash(plan) != _digest(row["plan_hash"]) or payment["network"] not in USDC
                or payment["asset"] != USDC[payment["network"]] or payment["scheme"] != "exact"
                or payment["pay_to"] != _address(payment["pay_to"]) or payment["valid_after"] != 0
                or type(payment["valid_before"]) is not int or payment["valid_before"] != snapshot["expires"]
                or snapshot["expires"] - snapshot["created"] != TTL_SECONDS
                or price["currency"] != "USD" or price["discount_bps"] != 0
                or type(rate) not in {int, float} or not math.isfinite(rate) or rate <= 0
                or type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0
                or math.ceil(rate * seconds * pricing.MICRO) != row["amount_micro"]
                or not isinstance(price["version"], str) or not 0 < len(price["version"]) <= 64
                or type(snapshot.get("preferred_worker", "")) is not str or len(snapshot.get("preferred_worker", "")) > 255
                or type(row["amount_micro"]) is not int or not 0 < row["amount_micro"] <= row["max_cost_micro"]
                or not _HASH.fullmatch(snapshot["salt"])):
            raise VideoQuoteError("invalid video quote binding")
        if snapshot["schema"] == "aipg.video-quote-binding.v2":
            if payment.get("eip712_domain") != payment_domain(payment["network"]):
                raise VideoQuoteError("video quote signing domain changed")
        elif "eip712_domain" in payment:
            raise VideoQuoteError("legacy quote cannot acquire a signing domain")
        for key in ("idempotency_hash", "request_hash", "commitment"):
            _digest(row[key])
        return snapshot
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise VideoQuoteError("invalid stored video quote") from exc


async def _lookup(session, account_id, *, quote_id=None, idempotency_key=None, request_hash=None) -> dict | None:
    if (quote_id is None) == (idempotency_key is None):
        raise VideoQuoteError("exactly one quote selector required")
    family = await identities.account_family_ids(account_id, session=session, max_members=128)
    selector = (video_quotes.c.id == _uuid(quote_id) if quote_id is not None
                else video_quotes.c.idempotency_hash == _key_hash(idempotency_key))
    rows = (await session.execute(sa.select(video_quotes).where(video_quotes.c.account_id.in_(family), selector)
                                  .limit(2))).mappings().all()
    if len(rows) > 1:
        raise VideoQuoteConflict("quote identity is ambiguous after account merge")
    if not rows:
        return None
    row = dict(rows[0])
    _validate_row(row)
    if request_hash is not None and row["request_hash"] != _digest(request_hash):
        raise VideoQuoteConflict("quote identity belongs to another intent")
    return row


async def lookup(account_id, *, quote_id=None, idempotency_key=None, request_hash=None) -> dict | None:
    async with await new_session() as session:
        return await _lookup(session, account_id, quote_id=quote_id, idempotency_key=idempotency_key,
                             request_hash=request_hash)


def _limits(per_account_limit: int, global_limit: int) -> None:
    if (type(per_account_limit) is not int or not 1 <= per_account_limit <= 32
            or type(global_limit) is not int or not per_account_limit <= global_limit <= 4096):
        raise VideoQuoteError("invalid retained quote capacity")


async def _capacity(session, account_id, per_account_limit: int, global_limit: int) -> None:
    _limits(per_account_limit, global_limit)
    family = await identities.account_family_ids(account_id, session=session, max_members=128)
    own = await session.scalar(sa.select(sa.func.count()).select_from(video_quotes)
                               .where(video_quotes.c.account_id.in_(family)))
    total = await session.scalar(sa.select(sa.func.count()).select_from(video_quotes))
    if own >= per_account_limit or total >= global_limit:
        raise VideoQuoteCapacity("retained video quote capacity exhausted")


async def check_capacity(account_id, *, per_account_limit: int, global_limit: int) -> None:
    """Cheap preflight before decoding; the locked insert rechecks all races."""
    async with await new_session() as session:
        await _capacity(session, account_id, per_account_limit, global_limit)


async def persist(quote: PreparedVideoQuote, *, idempotency_key: str,
                  per_account_limit: int, global_limit: int,
                  admission_until: datetime | None = None) -> tuple[dict, bool]:
    """Insert once, with global SQL capacity serialization; no I/O under locks."""
    _limits(per_account_limit, global_limit)
    row = quote.values
    _validate_row(row)
    if _key_hash(idempotency_key) != row["idempotency_hash"]:
        raise VideoQuoteError("quote key binding changed")
    async with await new_session() as session:
        if session.bind.dialect.name == "postgresql":
            await session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK})
        canonical = await identities.canonical_account_id(row["account_id"], session=session)
        locked = (await session.execute(sa.select(accounts.c.id).where(
            accounts.c.id.in_({row["account_id"], canonical}),
        ).order_by(accounts.c.id).with_for_update())).scalars().all()
        if canonical not in locked:
            raise VideoQuoteError("quote owner unavailable")
        if await identities.canonical_account_id(row["account_id"], session=session) != canonical:
            raise VideoQuoteConflict("quote owner changed during admission")
        existing = await _lookup(session, canonical, idempotency_key=idempotency_key, request_hash=row["request_hash"])
        if existing is not None:
            return existing, False
        if canonical != row["account_id"]:
            raise VideoQuoteConflict("new quotes require the canonical account")
        if admission_until is not None and _aware(admission_until) <= _now():
            raise VideoQuoteError("video quote pilot expired before commit")
        if _aware(row["expires"]) <= _now():
            raise VideoQuoteError("video quote expired before commit")
        await _capacity(session, canonical, per_account_limit, global_limit)
        await session.execute(video_quotes.insert().values(**row))
        await session.commit()
        return row, True


def view(row: dict) -> dict:
    snapshot = _validate_row(row)
    plan = snapshot["plan"]
    return {
        "schema": "aipg.video-quote.v1", "quote_id": str(row["id"]), "job_id": str(row["job_id"]),
        "binding_schema": snapshot["schema"],
        "state": "expired" if _aware(row["expires"]) <= _now() else "quoted", "payment_enabled": False,
        "request_hash": row["request_hash"], "plan_hash": row["plan_hash"], "commitment": row["commitment"],
        "authorization_nonce": row["authorization_nonce"], "payment": dict(snapshot["payment"]),
        "price": dict(snapshot["price"]), "max_cost_micro_usd": row["max_cost_micro"],
        "model": plan["model"], "seed": plan["payload"]["seed"], "execution": plan["execution"],
        "inputs": plan["inputs"], "recipe_root": plan["payload"]["recipe_root"],
        "created": snapshot["created"], "expires": snapshot["expires"],
    }
