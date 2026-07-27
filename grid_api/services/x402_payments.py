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
``verified`` payment row before dispatch and the SDK after-settle hook records
the transfer. Worker payout queries exclude the job until that row is settled.
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
from datetime import UTC, datetime
from urllib.parse import urlparse

import sqlalchemy as sa

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
                    payments_t.c.job_id == str(job_id),
                ),
            ),
        ).first()
        return int(row[0]) if row and row[0] is not None else None


async def _update_from_hook(context, *, status: str, error: str | None = None) -> None:
    transport = getattr(context, "transport_context", None)
    headers = getattr(transport, "response_headers", None) or {}
    job_id = next(
        (value for key, value in headers.items() if key.lower() == "x-grid-job-id"),
        None,
    )
    if not job_id:
        logger.error("x402 %s hook missing X-Grid-Job-ID", status)
        return

    values: dict = {"status": status, "error": (error or "")[:255] or None}
    if status == "settled":
        result = context.result
        values.update(
            settled_micro=int(context.requirements.amount),
            tx_hash=str(result.transaction),
            settled=_now(),
            error=None,
        )
    try:
        async with await new_session() as session:
            updated = await session.execute(
                sa.update(payments_t).where(payments_t.c.job_id == str(job_id)).values(**values),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"x402 payment row missing for job {job_id}")
            await session.commit()
    except Exception:
        logger.exception("x402 %s could not be persisted job=%s", status, job_id)
        alerts.emit(
            "x402_receipt_persist_failed",
            "critical",
            "An x402 facilitator result could not be persisted.",
            fields={"job": alerts.opaque_id(job_id), "status": status},
            dedupe_key=f"x402-receipt:{alerts.opaque_id(job_id)}",
        )
        raise


async def _after_settle(context) -> None:
    await _update_from_hook(context, status="settled")


async def _on_settle_failure(context):
    await _update_from_hook(context, status="failed", error=str(context.error))
    return None


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
