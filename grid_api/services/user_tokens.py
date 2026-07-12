# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Core-issued, short-lived user access tokens.

Service keys authenticate applications. These tokens authenticate a canonical
user and carry only the authority granted by the proof used at exchange time.
They are intentionally not persisted as API keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException

PREFIX = "gridu_"
ISSUER = "grid-core"
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600
STEP_UP_MAX_AGE_SECONDS = 600


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _secret() -> bytes:
    value = os.getenv("GRID_USER_TOKEN_SIGNING_KEY", "").strip()
    if len(value) < 32:
        raise HTTPException(503, detail="Native Grid user sessions are not configured")
    return value.encode()


def issue(
    account_id,
    *,
    audience: str,
    scopes: list[str],
    auth_method: str,
    service_id: str | None = None,
    lifetime_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    ttl = max(60, min(int(lifetime_seconds), MAX_TTL_SECONDS))
    payload = {
        "iss": ISSUER,
        "sub": str(account_id),
        "aud": audience,
        "scopes": sorted(set(scopes)),
        "amr": auth_method,
        "service_id": service_id,
        "auth_time": issued,
        "iat": issued,
        "exp": issued + ttl,
        "jti": secrets.token_urlsafe(18),
    }
    body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _encode(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{PREFIX}{body}.{signature}"


def verify(token: str, *, audience: str | None = None, now: int | None = None) -> dict:
    if not token or not token.startswith(PREFIX) or len(token) > 4096:
        raise HTTPException(401, detail="Malformed Grid user token")
    try:
        body, supplied_signature = token[len(PREFIX) :].split(".", 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(supplied_signature), expected):
            raise ValueError("signature")
        payload = json.loads(_decode(body))
        issued = int(payload["iat"])
        expires = int(payload["exp"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, detail="Invalid Grid user token")

    current = int(time.time() if now is None else now)
    if payload.get("iss") != ISSUER or issued > current + 5 or expires <= current:
        raise HTTPException(401, detail="Expired or invalid Grid user token")
    if expires - issued > MAX_TTL_SECONDS or not payload.get("sub") or not payload.get("jti"):
        raise HTTPException(401, detail="Invalid Grid user token claims")
    if audience is not None and payload.get("aud") != audience:
        raise HTTPException(401, detail="Grid user token audience mismatch")
    if not isinstance(payload.get("scopes"), list):
        raise HTTPException(401, detail="Invalid Grid user token scopes")
    return payload


def require_recent_step_up(claims: dict, *, now: int | None = None) -> None:
    current = int(time.time() if now is None else now)
    if claims.get("amr") not in {"google", "siwe"}:
        raise HTTPException(403, detail="Account management requires Google or wallet proof")
    try:
        age = current - int(claims["auth_time"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(403, detail="Fresh account proof required")
    if age < 0 or age > STEP_UP_MAX_AGE_SECONDS:
        raise HTTPException(403, detail="Account proof is stale; sign in again")
