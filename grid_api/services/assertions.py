# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Short-lived, replay-safe identity assertions from scoped frontend bridges."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from fastapi import HTTPException

from . import identities

AUDIENCE = "grid-core"
MAX_LIFETIME_SECONDS = 60
_REPLAY_PREFIX = "grid:identity_assertion:"
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def key_issuer(plain_key: str) -> str:
    return hashlib.sha256(plain_key.encode()).hexdigest()[:24]


def sign(plain_key: str, *, provider: str, subject: str, nonce: str | None = None,
         lifetime_seconds: int = 45, now: int | None = None) -> str:
    """Reference signer for trusted server-side bridges; never call in a browser."""
    issued = int(now if now is not None else time.time())
    lifetime = max(1, min(int(lifetime_seconds), MAX_LIFETIME_SECONDS))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": key_issuer(plain_key), "sub": subject, "provider": provider,
        "aud": AUDIENCE, "iat": issued, "exp": issued + lifetime,
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    encoded = f"{_b64encode(_json(header))}.{_b64encode(_json(payload))}"
    signature = hmac.new(plain_key.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


async def verify(plain_key: str, bridge_user: dict, token: str) -> dict:
    """Verify and consume one assertion, then return its canonical identity."""
    if "identity.assert" not in set(bridge_user.get("scopes") or []):
        raise HTTPException(403, detail="API key is not an identity bridge")
    if not token or len(token) > 4096:
        raise HTTPException(401, detail="Malformed user assertion")
    try:
        head_raw, payload_raw, signature_raw = token.split(".")
        header = json.loads(_b64decode(head_raw))
        payload = json.loads(_b64decode(payload_raw))
        signature = _b64decode(signature_raw)
    except Exception:
        raise HTTPException(401, detail="Malformed user assertion")
    if header != {"alg": "HS256", "typ": "JWT"}:
        raise HTTPException(401, detail="Unsupported user assertion")
    expected = hmac.new(
        plain_key.encode(), f"{head_raw}.{payload_raw}".encode(), hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, detail="Invalid user assertion signature")

    now = int(time.time())
    try:
        issued, expires = int(payload["iat"]), int(payload["exp"])
        provider = str(payload["provider"])
        subject = str(payload["sub"])
        nonce = str(payload["nonce"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(401, detail="Incomplete user assertion")
    if payload.get("aud") != AUDIENCE or payload.get("iss") != key_issuer(plain_key):
        raise HTTPException(401, detail="User assertion audience or issuer mismatch")
    if issued > now + 5 or expires <= now or expires - issued > MAX_LIFETIME_SECONDS:
        raise HTTPException(401, detail="Expired or invalid user assertion lifetime")
    if provider not in {"google", "wallet"} or not subject or len(subject) > 255:
        raise HTTPException(401, detail="Unsupported asserted identity")
    if provider == "wallet" and not _WALLET_RE.fullmatch(subject):
        raise HTTPException(401, detail="Malformed asserted wallet")
    if len(nonce) < 16 or len(nonce) > 128:
        raise HTTPException(401, detail="Invalid user assertion nonce")

    # Auth must fail closed when replay protection is unavailable.
    try:
        from ..redis_client import get_redis

        fresh = await get_redis().set(
            f"{_REPLAY_PREFIX}{payload['iss']}:{nonce}", "1",
            nx=True, ex=MAX_LIFETIME_SECONDS + 10,
        )
    except Exception:
        raise HTTPException(503, detail="Identity assertion replay protection unavailable")
    if not fresh:
        raise HTTPException(401, detail="User assertion already used")
    return {"provider": provider, "subject": subject, "nonce": nonce}
