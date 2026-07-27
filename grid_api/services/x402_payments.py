# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gated x402 USDC settlement for accountless agent requests.

The first production surface is deliberately narrow:

* Base USDC only;
* the x402 ``upto`` scheme, so the payer authorizes a fixed ceiling while the
  Grid settles only trusted, grid-counted usage;
* non-streaming OpenAI chat only, because the upstream FastAPI middleware
  buffers response bodies before settlement;
* disabled unless every required operator setting is present.

An x402 signature is authorization, not revenue. The request handler writes a
``verified`` payment row before dispatch. A before-settle hook durably records
the exact attempted amount before the facilitator can touch chain. Facilitator
success is only ``reported``; Core promotes it to ``settled`` after independently
proving the exact canonical-USDC transfer on Base. Worker payout queries exclude
every other state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import HTTPException

from ..database import new_session
from ..v2.schema import x402_payments as payments_t
from . import alerts

logger = logging.getLogger("grid_api.x402")

ENABLED = os.getenv("GRID_X402_ENABLED", "0").lower() in ("1", "true", "yes", "on")
NETWORK = os.getenv("GRID_X402_NETWORK", "eip155:8453").strip()
FACILITATOR_URL = (
    os.getenv(
        "GRID_X402_FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )
    .strip()
    .rstrip("/")
)
CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID", "").strip()
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET", "").strip()
PAY_TO = (os.getenv("GRID_X402_PAY_TO", "") or os.getenv("GRID_USDC_TREASURY", "")).strip().lower()
USDC = (
    os.getenv(
        "GRID_USDC_CONTRACT",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )
    .strip()
    .lower()
)
MAX_AUTH_MICRO = max(1, int(os.getenv("GRID_X402_MAX_AUTH_MICRO", "1000000") or 1_000_000))
DEFAULT_MAX_TOKENS = max(
    1,
    int(os.getenv("GRID_X402_DEFAULT_MAX_TOKENS", "4096") or 4096),
)
STALE_SECONDS = max(
    300,
    int(os.getenv("GRID_X402_STALE_SECONDS", "900") or 900),
)
ROUTE = "/v1/x402/chat/completions"


def _now() -> datetime:
    return datetime.now(UTC)


def _address(value: str, name: str) -> str:
    value = (value or "").strip().lower()
    if len(value) != 42 or not value.startswith("0x"):
        raise RuntimeError(f"{name} must be a 20-byte EVM address")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a hexadecimal EVM address") from exc
    return value


def validate_config() -> None:
    """Fail startup when an enabled money rail is only half configured."""
    if not ENABLED:
        return
    if NETWORK not in {"eip155:8453", "eip155:84532"}:
        raise RuntimeError("GRID_X402_NETWORK must be Base mainnet or Base Sepolia")
    _address(PAY_TO, "GRID_X402_PAY_TO/GRID_USDC_TREASURY")
    _address(USDC, "GRID_USDC_CONTRACT")
    parsed = urlparse(FACILITATOR_URL)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("GRID_X402_FACILITATOR_URL must be an https URL")
    if NETWORK == "eip155:8453" and not (CDP_API_KEY_ID and CDP_API_KEY_SECRET):
        raise RuntimeError("Base-mainnet x402 requires CDP_API_KEY_ID and CDP_API_KEY_SECRET")


def _load_cdp_key(secret: str):
    """Load either a PEM P-256 key or Coinbase's base64 Ed25519 secret."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    normalized = secret.replace("\\n", "\n")
    try:
        key = serialization.load_pem_private_key(normalized.encode(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return key, "ES256"
    except (TypeError, ValueError):
        pass
    try:
        raw = base64.b64decode(normalized, validate=True)
        if len(raw) == 64:
            return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32]), "EdDSA"
    except (ValueError, TypeError):
        pass
    raise RuntimeError("CDP_API_KEY_SECRET must be a PEM EC key or base64 Ed25519 key")


def _cdp_jwt(method: str, path: str) -> str:
    """Create the short-lived, request-bound bearer token required by CDP."""
    import jwt

    key, algorithm = _load_cdp_key(CDP_API_KEY_SECRET)
    host = urlparse(FACILITATOR_URL).netloc
    now = int(time.time())
    claims = {
        "sub": CDP_API_KEY_ID,
        "iss": "cdp",
        "aud": None,
        "nbf": now,
        "exp": now + 120,
        "uris": [f"{method} {host}{path}"],
    }
    headers = {
        "alg": algorithm,
        "kid": CDP_API_KEY_ID,
        "typ": "JWT",
        "nonce": f"{secrets.randbelow(10**16):016d}",
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


def _facilitator_headers() -> dict[str, dict[str, str]]:
    """Header callback expected by x402's HTTP facilitator client."""
    base_path = urlparse(FACILITATOR_URL).path.rstrip("/")
    common = {
        "Content-Type": "application/json",
        "Correlation-Context": "source=aipg-grid,source_version=1",
    }
    if not (CDP_API_KEY_ID and CDP_API_KEY_SECRET):
        return {key: dict(common) for key in ("verify", "settle", "supported", "list")}

    def authenticated(method: str, suffix: str) -> dict[str, str]:
        path = f"{base_path}/{suffix}"
        return {**common, "Authorization": f"Bearer {_cdp_jwt(method, path)}"}

    return {
        "verify": authenticated("POST", "verify"),
        "settle": authenticated("POST", "settle"),
        "supported": authenticated("GET", "supported"),
        "list": dict(common),
    }


def payment_payload_details(payload, requirements) -> dict:
    """Extract verified EVM payer and immutable requirement details."""
    raw = payload.payload if hasattr(payload, "payload") else {}
    auth = raw.get("permit2Authorization") if isinstance(raw, dict) else None
    payer = (auth or {}).get("from", "")
    payer = _address(payer, "x402 payer")
    nonce = str((auth or {}).get("nonce", "")).strip()
    if not nonce:
        raise RuntimeError("x402 Permit2 authorization nonce is required")
    authorization_id = hashlib.sha256(
        json.dumps(
            {
                "payer": payer,
                "network": str(requirements.network),
                "asset": str(requirements.asset).lower(),
                "nonce": nonce,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    return {
        "authorization_id": authorization_id,
        "payer": payer,
        "network": str(requirements.network),
        "asset": _address(str(requirements.asset), "x402 asset"),
        "pay_to": _address(str(requirements.pay_to), "x402 payTo"),
        "authorized_micro": int(requirements.amount),
    }


async def insert_verified_in_session(session, *, job_id: str, details: dict) -> None:
    """Persist verified authorization in the caller's reservation transaction."""
    await session.execute(
        sa.insert(payments_t).values(
            job_id=str(job_id),
            authorization_id=details["authorization_id"],
            payer=details["payer"],
            network=details["network"],
            asset=details["asset"],
            pay_to=details["pay_to"],
            authorized_micro=int(details["authorized_micro"]),
            status="verified",
            created=_now(),
        ),
    )


async def settled_amount(job_id: str) -> int | None:
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(payments_t.c.settled_micro).where(
                    sa.and_(
                        payments_t.c.job_id == str(job_id),
                        payments_t.c.status == "settled",
                    ),
                ),
            )
        ).first()
        return int(row[0]) if row and row[0] is not None else None


def _job_id_from_context(context) -> str | None:
    transport = getattr(context, "transport_context", None)
    headers = getattr(transport, "response_headers", None) or {}
    return next(
        (value for key, value in headers.items() if key.lower() == "x-grid-job-id"),
        None,
    )


async def _before_settle(context) -> None:
    """Persist the exact attempted amount before any facilitator side effect."""
    job_id = _job_id_from_context(context)
    if not job_id:
        raise RuntimeError("x402 before-settle hook missing X-Grid-Job-ID")
    amount = int(context.requirements.amount)
    if amount <= 0:
        raise RuntimeError("x402 settlement amount must be positive")

    async with await new_session() as session:
        updated = await session.execute(
            sa.update(payments_t)
            .where(
                sa.and_(
                    payments_t.c.job_id == str(job_id),
                    payments_t.c.status == "verified",
                    amount <= payments_t.c.authorized_micro,
                ),
            )
            .values(
                status="settling",
                settled_micro=amount,
                attempts=payments_t.c.attempts + 1,
                last_attempt=_now(),
                error=None,
            ),
        )
        if updated.rowcount != 1:
            await session.rollback()
            raise RuntimeError(
                f"x402 payment {job_id} is missing, already attempted, or under-authorized",
            )
        await session.commit()


async def _after_settle(context) -> None:
    job_id = _job_id_from_context(context)
    if not job_id:
        raise RuntimeError("x402 after-settle hook missing X-Grid-Job-ID")
    result = context.result
    tx_hash = str(result.transaction or "").lower()
    if not (
        tx_hash.startswith("0x")
        and len(tx_hash) == 66
        and all(char in "0123456789abcdef" for char in tx_hash[2:])
    ):
        raise RuntimeError("x402 facilitator success omitted a valid transaction hash")
    try:
        async with await new_session() as session:
            updated = await session.execute(
                sa.update(payments_t)
                .where(
                    sa.and_(
                        payments_t.c.job_id == str(job_id),
                        payments_t.c.status == "settling",
                    ),
                )
                .values(
                    status="reported",
                    tx_hash=tx_hash,
                    settled=None,
                    error=None,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"x402 payment {job_id} was not in settling state")
            await session.commit()
    except Exception:
        logger.exception("x402 settled receipt could not be persisted job=%s", job_id)
        alerts.emit(
            "x402_receipt_persist_failed",
            "critical",
            "An x402 facilitator receipt could not be persisted.",
            fields={"job": alerts.opaque_id(job_id), "status": "settling"},
            dedupe_key=f"x402-receipt:{alerts.opaque_id(job_id)}",
        )
        raise


async def _on_settle_failure(context):
    job_id = _job_id_from_context(context)
    if not job_id:
        logger.error("x402 failure hook missing X-Grid-Job-ID")
        return None
    error = str(context.error)[:255] or "settlement failed"
    try:
        async with await new_session() as session:
            # Only a call that crossed the durable before-settle boundary is
            # ambiguous. A failure before that boundary leaves `verified`
            # retryable and cannot have called the facilitator.
            await session.execute(
                sa.update(payments_t)
                .where(
                    sa.and_(
                        payments_t.c.job_id == str(job_id),
                        payments_t.c.status == "settling",
                    ),
                )
                .values(status="manual_review", error=error),
            )
            await session.commit()
    except Exception:
        logger.exception("x402 failure state could not be persisted job=%s", job_id)
        alerts.emit(
            "x402_failure_persist_failed",
            "critical",
            "An ambiguous x402 settlement failure could not be persisted.",
            fields={"job": alerts.opaque_id(job_id)},
            dedupe_key=f"x402-failure:{alerts.opaque_id(job_id)}",
        )
        raise
    return None


async def flag_stale_settlements(older_than_seconds: int | None = None) -> int:
    """Move abandoned `settling`/`reported` rows to manual review."""
    cutoff = _now() - timedelta(seconds=older_than_seconds or STALE_SECONDS)
    async with await new_session() as session:
        updated = await session.execute(
            sa.update(payments_t)
            .where(
                sa.and_(
                    payments_t.c.status.in_(("settling", "reported")),
                    payments_t.c.last_attempt < cutoff,
                ),
            )
            .values(
                status="manual_review",
                error="settlement outcome not independently proven before timeout",
            ),
        )
        await session.commit()
        count = int(updated.rowcount or 0)
    if count:
        alerts.emit(
            "x402_settlement_stale",
            "critical",
            "x402 settlements require on-chain operator reconciliation.",
            fields={"count": count},
            dedupe_key="x402-settlement-stale",
        )
    return count


async def verify_reported_settlements(limit: int = 50) -> dict[str, int]:
    """Promote facilitator reports only after exact Base transfer proof."""
    limit = max(1, min(int(limit or 50), 250))
    async with await new_session() as session:
        rows = (
            await session.execute(
                sa.select(payments_t.c.job_id, payments_t.c.tx_hash)
                .where(payments_t.c.status == "reported")
                .order_by(payments_t.c.last_attempt)
                .limit(limit),
            )
        ).all()
    outcome = {"settled": 0, "pending": 0, "manual_review": 0}
    for job_id, tx_hash in rows:
        try:
            await reconcile_transaction(str(job_id), str(tx_hash))
            outcome["settled"] += 1
        except HTTPException:
            # Not mined, not sufficiently confirmed, or RPC unavailable. Keep
            # the report payout-ineligible and try again until the stale gate.
            outcome["pending"] += 1
        except RuntimeError as exc:
            async with await new_session() as session:
                updated = await session.execute(
                    sa.update(payments_t)
                    .where(
                        sa.and_(
                            payments_t.c.job_id == str(job_id),
                            payments_t.c.status == "reported",
                        ),
                    )
                    .values(status="manual_review", error=str(exc)[:255]),
                )
                await session.commit()
            if updated.rowcount:
                outcome["manual_review"] += 1
                alerts.emit(
                    "x402_report_unproven",
                    "critical",
                    "A facilitator-reported x402 transfer failed independent Base verification.",
                    fields={"job": alerts.opaque_id(job_id)},
                    dedupe_key=f"x402-unproven:{alerts.opaque_id(job_id)}",
                )
    return outcome


async def reconcile_transaction(job_id: str, tx_hash: str) -> dict:
    """Prove an ambiguous x402 payment from its confirmed Base USDC transfer.

    This is intentionally an operator path, not an automatic retry. The
    transaction must contain canonical-USDC transfers from the recorded payer
    to the recorded recipient totaling the exact grid-counted charge.
    """
    from ..v2.schema import reservations as reservations_t
    from . import deposits

    job_id = str(job_id).strip()
    tx_hash = deposits._normalize_tx_hash(tx_hash)
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(
                    payments_t.c.payer,
                    payments_t.c.asset,
                    payments_t.c.pay_to,
                    payments_t.c.authorized_micro,
                    payments_t.c.settled_micro,
                    payments_t.c.tx_hash,
                    payments_t.c.status,
                    payments_t.c.last_attempt,
                    reservations_t.c.actual_micro,
                )
                .join(
                    reservations_t,
                    reservations_t.c.job_id == payments_t.c.job_id,
                )
                .where(payments_t.c.job_id == job_id),
            )
        ).mappings().first()
    if not row:
        raise RuntimeError("x402 payment job was not found")
    if row["status"] == "settled":
        if (row["tx_hash"] or "").lower() != tx_hash:
            raise RuntimeError("x402 payment is already settled by another transaction")
        return {
            "job_id": job_id,
            "status": "settled",
            "tx_hash": tx_hash,
            "already_reconciled": True,
        }

    actual = int(row["actual_micro"] or 0)
    attempted = int(row["settled_micro"] or 0)
    if actual <= 0 or attempted != actual:
        raise RuntimeError("x402 payment has no exact terminal charge to reconcile")
    if actual > int(row["authorized_micro"]):
        raise RuntimeError("x402 terminal charge exceeds its authorization")
    attempted_at = row["last_attempt"]
    if attempted_at is None:
        raise RuntimeError("x402 payment has no durable settlement attempt")
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=UTC)

    _tx, receipt, block_number = await deposits._confirmed_transaction(
        tx_hash,
        "x402 USDC",
    )
    block_time = await deposits._block_timestamp(block_number)
    if block_time < attempted_at - timedelta(minutes=5) or block_time > _now() + timedelta(minutes=5):
        raise RuntimeError("confirmed transaction is outside the x402 settlement window")
    received = deposits._direct_erc20_amount(
        receipt,
        str(row["asset"]).lower(),
        str(row["pay_to"]).lower(),
        str(row["payer"]).lower(),
    )
    if received != actual:
        raise RuntimeError(
            "confirmed transaction does not prove the exact x402 USDC transfer",
        )

    async with await new_session() as session:
        try:
            updated = await session.execute(
                sa.update(payments_t)
                .where(
                    sa.and_(
                        payments_t.c.job_id == job_id,
                        payments_t.c.status != "settled",
                        payments_t.c.settled_micro == actual,
                    ),
                )
                .values(
                    status="settled",
                    tx_hash=tx_hash,
                    settled=_now(),
                    error=None,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("x402 payment changed during reconciliation")
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    alerts.emit(
        "x402_payment_reconciled",
        "success",
        "An ambiguous x402 payment was reconciled from a confirmed Base transfer.",
        fields={"job": alerts.opaque_id(job_id), "tx": alerts.opaque_id(tx_hash)},
        dedupe_key=f"x402-reconciled:{alerts.opaque_id(job_id)}",
    )
    return {
        "job_id": job_id,
        "status": "settled",
        "tx_hash": tx_hash,
        "settled_micro": actual,
        "already_reconciled": False,
    }


@dataclass(frozen=True)
class X402Runtime:
    server: object
    routes: dict


def build_runtime() -> X402Runtime:
    """Build the official x402 resource server after strict config validation."""
    validate_config()
    from x402 import x402ResourceServer
    from x402.http import HTTPFacilitatorClient
    from x402.http.types import PaymentOption, RouteConfig
    from x402.mechanisms.evm.upto import UptoEvmServerScheme

    facilitator = HTTPFacilitatorClient(
        {"url": FACILITATOR_URL, "create_headers": _facilitator_headers},
    )
    server = x402ResourceServer(facilitator)
    server.register(NETWORK, UptoEvmServerScheme())
    server.on_before_settle(_before_settle)
    server.on_after_settle(_after_settle)
    server.on_settle_failure(_on_settle_failure)
    routes = {
        f"POST {ROUTE}": RouteConfig(
            accepts=PaymentOption(
                scheme="upto",
                pay_to=PAY_TO,
                price={"amount": str(MAX_AUTH_MICRO), "asset": USDC},
                network=NETWORK,
                max_timeout_seconds=300,
            ),
            resource=ROUTE,
            description="Non-streaming OpenAI-compatible Grid inference paid in Base USDC.",
            mime_type="application/json",
            service_name="AI Power Grid",
            tags=["ai", "inference", "base", "usdc"],
        ),
    }
    return X402Runtime(server=server, routes=routes)


def install_middleware(app) -> None:
    """Install no middleware at all while dark; enabled misconfig fails startup."""
    if not ENABLED:
        return
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI

    runtime = build_runtime()
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes=runtime.routes,
        server=runtime.server,
    )


async def _run_reconcile_cli(job_id: str, tx_hash: str) -> None:
    from ..database import close_database, init_database

    await init_database()
    try:
        result = await reconcile_transaction(job_id, tx_hash)
        print(json.dumps(result, sort_keys=True))
    finally:
        await close_database()


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Reconcile one ambiguous x402 job from a confirmed Base transaction.",
    )
    parser.add_argument("--reconcile-job", required=True, help="Grid job UUID")
    parser.add_argument("--tx", required=True, help="Base transaction hash")
    args = parser.parse_args()
    asyncio.run(_run_reconcile_cli(args.reconcile_job, args.tx))
