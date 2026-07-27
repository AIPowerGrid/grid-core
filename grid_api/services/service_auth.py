# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded service accounts and native provider-proof exchange."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from fastapi import HTTPException

from ..database import new_session
from ..v2.schema import service_clients, service_events
from . import user_tokens

_SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
logger = logging.getLogger("grid_api.service_auth")


def normalize_service_id(value: str) -> str:
    service_id = (value or "").strip().lower()
    if not _SERVICE_ID_RE.fullmatch(service_id):
        raise ValueError("service id must be 3..64 lowercase letters, digits, or hyphens")
    return service_id


def normalize_siwe_domains(values: list[str] | None) -> list[str]:
    """Normalize exact EIP-4361 authorities; schemes and paths are forbidden."""
    domains: set[str] = set()
    for raw in values or []:
        domain = str(raw or "").strip().lower()
        parsed = urlsplit(f"//{domain}")
        if (
            not domain
            or parsed.netloc != domain
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "@" in domain
        ):
            raise ValueError("SIWE domains must be exact lowercase host authorities")
        domains.add(domain)
    return sorted(domains)


def _identity_policy(row: dict) -> dict:
    return {
        "allowed_providers": sorted(set(row.get("allowed_providers") or [])),
        "google_audiences": sorted(set(row.get("google_audiences") or [])),
        "siwe_domains": normalize_siwe_domains(row.get("siwe_domains") or []),
    }


def identity_policy_digest(policy: dict) -> str:
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def configure_identity_policy(
    service_id: str,
    *,
    allowed_providers: list[str],
    google_audiences: list[str] | None = None,
    siwe_domains: list[str] | None = None,
    expected_digest: str | None = None,
    apply: bool = False,
) -> dict:
    """Preview or compare-and-swap one service's provider proof policy."""
    sid = normalize_service_id(service_id)
    providers = sorted(set(allowed_providers))
    if not providers or not set(providers).issubset({"app", "google", "wallet"}):
        raise ValueError("service providers must be app, google, and/or wallet")
    desired = {
        "allowed_providers": providers,
        "google_audiences": sorted(set(google_audiences or [])),
        "siwe_domains": normalize_siwe_domains(siwe_domains),
    }
    if "google" in providers and not desired["google_audiences"]:
        raise ValueError("Google provider requires at least one audience")
    if "wallet" in providers and not desired["siwe_domains"]:
        raise ValueError("wallet provider requires at least one SIWE domain")

    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(service_clients)
                .where(service_clients.c.id == sid)
                .with_for_update(),
            )
        ).mappings().first()
        if not row:
            raise ValueError("service client not found")
        current = _identity_policy(dict(row))
        current_digest = identity_policy_digest(current)
        result = {
            "service_id": sid,
            "current": current,
            "current_digest": current_digest,
            "desired": desired,
            "changed": current != desired,
            "applied": False,
        }
        if not apply:
            await session.rollback()
            return result
        if not expected_digest or expected_digest.lower() != current_digest:
            await session.rollback()
            raise ValueError("service policy changed; preview again and pass its exact digest")
        if current == desired:
            await session.rollback()
            return result
        now = datetime.now(timezone.utc)
        await session.execute(
            sa.update(service_clients)
            .where(service_clients.c.id == sid)
            .values(**desired),
        )
        await session.execute(
            sa.insert(service_events).values(
                service_id=sid,
                account_id=row["account_id"],
                event_type="policy_updated",
                ref=new_event_ref("service-policy", sid),
                event_metadata={
                    "before_digest": current_digest,
                    "after_digest": identity_policy_digest(desired),
                },
                created=now,
            ),
        )
        await session.commit()
        result["applied"] = True
        return result


async def get_client(service_id: str, *, session=None) -> dict | None:
    owns_session = session is None
    if owns_session:
        session = await new_session()
    try:
        row = (
            (
                await session.execute(
                    sa.select(service_clients).where(
                        service_clients.c.id == service_id,
                        service_clients.c.active.is_(True),
                    ),
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None
    finally:
        if owns_session:
            await session.close()


async def record_event(
    service_id: str,
    event_type: str,
    *,
    account_id=None,
    ref: str,
    metadata: dict | None = None,
) -> None:
    try:
        async with await new_session() as session:
            await session.execute(
                sa.insert(service_events).values(
                    service_id=service_id,
                    account_id=account_id,
                    event_type=event_type,
                    ref=ref,
                    event_metadata=metadata or {},
                    created=datetime.now(timezone.utc),
                ),
            )
            await session.commit()
    except Exception:
        # Authentication must not fail because audit telemetry was duplicated or
        # temporarily unavailable. Auth decisions themselves remain fail-closed.
        logger.warning(
            "service event write failed service=%s event=%s ref=%s",
            service_id,
            event_type,
            ref,
            exc_info=True,
        )


async def verify_google_id_token(raw_token: str, audiences: list[str]) -> dict:
    if not raw_token or len(raw_token) > 16_384:
        raise HTTPException(401, detail="Google ID token required")
    allowed = {str(value) for value in audiences if value}
    if not allowed:
        raise HTTPException(503, detail="This service has no Google audience configured")

    def _verify() -> dict:
        try:
            from google.auth.transport import requests
            from google.oauth2 import id_token
        except ImportError as exc:
            raise RuntimeError("google-auth is not installed") from exc
        # Verify signature, issuer, and time first. Audience is checked against
        # the service's explicit allowlist below because a service may have web
        # and native OAuth client IDs.
        return id_token.verify_oauth2_token(raw_token, requests.Request(), audience=None)

    try:
        claims = await asyncio.to_thread(_verify)
    except Exception:
        raise HTTPException(401, detail="Google identity verification failed")
    if str(claims.get("aud") or "") not in allowed:
        raise HTTPException(401, detail="Google token audience is not allowed for this service")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise HTTPException(401, detail="Google token has no subject")
    return {
        "subject": subject,
        "email": claims.get("email"),
        "email_verified": bool(claims.get("email_verified")),
        "name": claims.get("name"),
    }


def issue_user_token(
    account_id,
    *,
    service_id: str,
    auth_method: str,
    account_manage: bool = False,
) -> str:
    scopes = ["account.read", "inference.submit"]
    if account_manage:
        scopes.append("account.manage")
    return user_tokens.issue(
        account_id,
        audience=service_id,
        service_id=service_id,
        scopes=scopes,
        auth_method=auth_method,
    )


def new_event_ref(prefix: str, service_id: str) -> str:
    return f"{prefix}:{service_id}:{uuid4()}"
