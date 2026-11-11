# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dark OAuth 2.1 authorization server for the remote MCP resource.

The browser sees opaque request and authorization-code capabilities. Core stores
only their SHA-256 hashes. Public clients use S256 PKCE and receive short-lived,
resource-bound Grid user tokens. Client secrets are deliberately absent.

Refresh tokens are default-off (`GRID_OAUTH_REFRESH_TOKENS_ENABLED`). When on,
a client that requests the `offline_access` scope and is approved receives a
rotating refresh token: each use consumes it and returns a successor, and
presenting a consumed token revokes the whole session, because a replay means
a copy escaped. A session ends after 30 idle days or 180 days in total. Access
tokens stay stateless, so one issued before a revocation remains valid for up
to its 15-minute lifetime.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import oauth_authorizations, oauth_clients, oauth_refresh_tokens
from . import identities, user_tokens

ALLOWED_SCOPES = frozenset({"account.read", "inference.submit"})
AUTHORIZATION_REQUEST_TTL_SECONDS = 600
AUTHORIZATION_CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 900
OFFLINE_SCOPE = "offline_access"
REFRESH_TOKEN_IDLE_SECONDS = 30 * 24 * 60 * 60
REFRESH_SESSION_MAX_SECONDS = 180 * 24 * 60 * 60
REFRESH_TOKEN_PREFIX = "oauth_refresh_"
MAX_REDIRECT_URIS = 10
DEFAULT_AUTHORIZATION_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_UNUSED_CLIENT_RETENTION_SECONDS = 24 * 60 * 60

_CLIENT_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


@dataclass
class OAuthProtocolError(Exception):
    error: str
    description: str
    status_code: int = 400


def enabled() -> bool:
    return os.getenv("GRID_MCP_OAUTH_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def refresh_enabled() -> bool:
    return enabled() and os.getenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def supported_scopes() -> list[str]:
    return sorted(ALLOWED_SCOPES | ({OFFLINE_SCOPE} if refresh_enabled() else set()))


def supported_grant_types() -> list[str]:
    return ["authorization_code", "refresh_token"] if refresh_enabled() else ["authorization_code"]


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, detail="Not found")


def issuer() -> str:
    return os.getenv("GRID_OAUTH_ISSUER", "https://api.aipowergrid.io").rstrip("/")


def resource() -> str:
    return os.getenv("GRID_OAUTH_RESOURCE", issuer()).rstrip("/")


def console_authorize_url() -> str:
    return os.getenv(
        "GRID_OAUTH_CONSOLE_AUTHORIZE_URL",
        "https://console.aipowergrid.io/oauth/authorize",
    ).strip()


def mcp_service_id() -> str:
    return os.getenv("GRID_MCP_SERVICE_ID", "grid-mcp").strip()


def _hash_capability(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _loopback_hostname(hostname: str | None) -> bool:
    return hostname in {"127.0.0.1", "::1", "localhost"}


def validate_redirect_uri(value: str, *, application_type: str) -> str:
    redirect_uri = str(value or "").strip()
    if not redirect_uri or len(redirect_uri) > 2048:
        raise ValueError("redirect_uri must be 1..2048 characters")
    try:
        parsed = urlsplit(redirect_uri)
        parsed.port
    except ValueError as exc:
        raise ValueError("redirect_uri has an invalid port") from exc
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("redirect_uri must be an absolute URL without credentials or a fragment")
    if parsed.scheme == "http" and not (application_type == "native" and _loopback_hostname(parsed.hostname)):
        raise ValueError("HTTP redirect_uri is allowed only for native loopback clients")
    if "*" in redirect_uri:
        raise ValueError("redirect_uri wildcards are not allowed")
    return redirect_uri


def _redirect_matches(registered: str, supplied: str, *, application_type: str) -> bool:
    if registered == supplied:
        return True
    if application_type != "native":
        return False
    left, right = urlsplit(registered), urlsplit(supplied)
    if not (left.scheme == right.scheme == "http" and _loopback_hostname(left.hostname) and left.hostname == right.hostname):
        return False
    # RFC 8252 requires native loopback redirects to accept an ephemeral port.
    return left.path == right.path and left.query == right.query and left.fragment == right.fragment == ""


def normalize_scopes(scope: str) -> list[str]:
    values = sorted(set(str(scope or "").split()))
    authority = set(values) - {OFFLINE_SCOPE}
    if not authority or not set(values).issubset(supported_scopes()):
        raise OAuthProtocolError(
            "invalid_scope",
            "Only account.read and inference.submit may be requested"
            + (", optionally with offline_access" if refresh_enabled() else ""),
        )
    return values


def _append_query(url: str, values: list[tuple[str, str]]) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(values)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


async def register_client(metadata: dict) -> dict:
    application_type = str(metadata.get("application_type") or "native").strip()
    if application_type not in {"native", "web"}:
        raise ValueError("application_type must be native or web")
    name = str(metadata.get("client_name") or "MCP client").strip()
    if not _CLIENT_NAME_RE.fullmatch(name):
        raise ValueError("client_name must be 1..120 printable characters")
    raw_redirects = metadata.get("redirect_uris")
    if not isinstance(raw_redirects, list) or not raw_redirects:
        raise ValueError("redirect_uris must contain at least one URL")
    if len(raw_redirects) > MAX_REDIRECT_URIS:
        raise ValueError(f"redirect_uris may contain at most {MAX_REDIRECT_URIS} URLs")
    redirects = sorted(
        {validate_redirect_uri(value, application_type=application_type) for value in raw_redirects},
    )
    if metadata.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError("Only public clients with token_endpoint_auth_method=none are supported")
    grant_types = set(metadata.get("grant_types") or ["authorization_code"])
    if "authorization_code" not in grant_types or not grant_types.issubset(supported_grant_types()):
        raise ValueError(
            "Only the authorization_code grant"
            + (" and the refresh_token grant are supported" if refresh_enabled() else " is supported"),
        )
    if set(metadata.get("response_types") or ["code"]) != {"code"}:
        raise ValueError("Only the code response type is supported")

    now = datetime.now(UTC)
    for _ in range(3):
        client_id = "grid_oauth_" + secrets.token_urlsafe(24)
        try:
            async with await new_session() as session:
                await session.execute(
                    sa.insert(oauth_clients).values(
                        id=client_id,
                        name=name,
                        redirect_uris=redirects,
                        application_type=application_type,
                        active=True,
                        created=now,
                    ),
                )
                await session.commit()
            return {
                "client_id": client_id,
                "client_name": name,
                "redirect_uris": redirects,
                "application_type": application_type,
                "grant_types": sorted(grant_types),
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        except IntegrityError:
            continue
    raise HTTPException(503, detail="Could not allocate OAuth client")


async def create_authorization_request(
    *,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    requested_resource: str,
) -> str:
    if response_type != "code":
        raise OAuthProtocolError("unsupported_response_type", "Only response_type=code is supported")
    if not state or len(state) > 512:
        raise OAuthProtocolError("invalid_request", "state must be 1..512 characters")
    if code_challenge_method != "S256" or not _PKCE_CHALLENGE_RE.fullmatch(code_challenge or ""):
        raise OAuthProtocolError("invalid_request", "A valid S256 PKCE challenge is required")
    if str(requested_resource or "").rstrip("/") != resource():
        raise OAuthProtocolError("invalid_target", "The requested resource is not this MCP resource")
    scopes = normalize_scopes(scope)
    supplied_redirect = str(redirect_uri or "").strip()

    async with await new_session() as session:
        client = (
            (
                await session.execute(
                    sa.select(oauth_clients).where(
                        oauth_clients.c.id == client_id,
                        oauth_clients.c.active.is_(True),
                    ),
                )
            )
            .mappings()
            .first()
        )
        if not client:
            raise OAuthProtocolError("unauthorized_client", "Unknown or inactive client")
        try:
            validate_redirect_uri(
                supplied_redirect,
                application_type=client["application_type"],
            )
        except ValueError as exc:
            raise OAuthProtocolError("invalid_request", str(exc)) from exc
        if not any(
            _redirect_matches(
                registered,
                supplied_redirect,
                application_type=client["application_type"],
            )
            for registered in client["redirect_uris"]
        ):
            raise OAuthProtocolError("invalid_request", "redirect_uri is not registered")

        capability = "oauth_req_" + secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        await session.execute(
            sa.insert(oauth_authorizations).values(
                request_hash=_hash_capability(capability),
                client_id=client_id,
                redirect_uri=supplied_redirect,
                resource=resource(),
                scopes=scopes,
                state=state,
                code_challenge=code_challenge,
                status="pending",
                created=now,
                expires_at=now + timedelta(seconds=AUTHORIZATION_REQUEST_TTL_SECONDS),
            ),
        )
        await session.commit()
    return _append_query(console_authorize_url(), [("request", capability)])


async def inspect_authorization_request(capability: str) -> dict:
    request_hash = _hash_capability(str(capability or ""))
    now = datetime.now(UTC)
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(
                        oauth_authorizations,
                        oauth_clients.c.name.label("client_name"),
                    )
                    .join(oauth_clients, oauth_clients.c.id == oauth_authorizations.c.client_id)
                    .where(oauth_authorizations.c.request_hash == request_hash),
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(404, detail="Authorization request not found")
    if row["status"] != "pending":
        raise HTTPException(409, detail="Authorization request is already closed")
    expires_at = _utc(row["expires_at"])
    if expires_at <= now:
        raise HTTPException(410, detail="Authorization request expired")
    redirect = urlsplit(row["redirect_uri"])
    return {
        "client_id": row["client_id"],
        "client_name": row["client_name"],
        "redirect_host": redirect.hostname,
        "resource": row["resource"],
        "scopes": list(row["scopes"]),
        "expires_in": max(0, int((expires_at - now).total_seconds())),
    }


async def decide_authorization_request(
    capability: str,
    *,
    approve: bool,
    account_id,
    auth_method: str,
) -> str:
    request_hash = _hash_capability(str(capability or ""))
    now = datetime.now(UTC)
    canonical_id = await identities.canonical_account_id(account_id)
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(oauth_authorizations).where(oauth_authorizations.c.request_hash == request_hash).with_for_update(),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise HTTPException(404, detail="Authorization request not found")
        if row["status"] != "pending":
            raise HTTPException(409, detail="Authorization request is already closed")
        if _utc(row["expires_at"]) <= now:
            raise HTTPException(410, detail="Authorization request expired")

        if not approve:
            await session.execute(
                sa.update(oauth_authorizations)
                .where(oauth_authorizations.c.request_hash == request_hash)
                .values(
                    status="denied",
                    account_id=canonical_id,
                    auth_method=auth_method,
                    decided_at=now,
                ),
            )
            await session.commit()
            return _append_query(
                row["redirect_uri"],
                [
                    ("error", "access_denied"),
                    ("error_description", "The user denied this authorization request"),
                    ("state", row["state"]),
                    ("iss", issuer()),
                ],
            )

        code = "oauth_code_" + secrets.token_urlsafe(32)
        await session.execute(
            sa.update(oauth_authorizations)
            .where(oauth_authorizations.c.request_hash == request_hash)
            .values(
                status="approved",
                account_id=canonical_id,
                auth_method=auth_method,
                code_hash=_hash_capability(code),
                decided_at=now,
                expires_at=now + timedelta(seconds=AUTHORIZATION_CODE_TTL_SECONDS),
            ),
        )
        await session.commit()
    return _append_query(
        row["redirect_uri"],
        [("code", code), ("state", row["state"]), ("iss", issuer())],
    )


async def exchange_authorization_code(
    *,
    grant_type: str,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    requested_resource: str,
) -> dict:
    if grant_type != "authorization_code":
        raise OAuthProtocolError("unsupported_grant_type", "Only authorization_code is supported")
    if str(requested_resource or "").rstrip("/") != resource():
        raise OAuthProtocolError("invalid_target", "The requested resource does not match the authorization")
    if not _PKCE_VERIFIER_RE.fullmatch(code_verifier or ""):
        raise OAuthProtocolError("invalid_grant", "Invalid authorization code or verifier")
    code_hash = _hash_capability(str(code or ""))
    now = datetime.now(UTC)

    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(oauth_authorizations).where(oauth_authorizations.c.code_hash == code_hash).with_for_update(),
                )
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] != "approved"
            or _utc(row["expires_at"]) <= now
            or row["client_id"] != client_id
            or row["redirect_uri"] != redirect_uri
            or row["resource"] != resource()
        ):
            raise OAuthProtocolError("invalid_grant", "Invalid or expired authorization code")
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest(),
            )
            .rstrip(b"=")
            .decode()
        )
        if not secrets.compare_digest(expected, row["code_challenge"]):
            raise OAuthProtocolError("invalid_grant", "Invalid authorization code or verifier")

        authority = sorted(set(row["scopes"]) - {OFFLINE_SCOPE})
        token = user_tokens.issue(
            row["account_id"],
            audience=resource(),
            scopes=authority,
            auth_method=row["auth_method"] or "app",
            client_id=client_id,
            lifetime_seconds=ACCESS_TOKEN_TTL_SECONDS,
        )
        refresh_token = None
        if OFFLINE_SCOPE in row["scopes"] and refresh_enabled():
            refresh_token = await _insert_refresh_token(
                session,
                family_id=secrets.token_urlsafe(24),
                client_id=client_id,
                account_id=row["account_id"],
                scopes=authority,
                auth_method=row["auth_method"] or "app",
                authorized_at=now,
                now=now,
            )
        await session.execute(
            sa.update(oauth_authorizations)
            .where(oauth_authorizations.c.request_hash == row["request_hash"])
            .values(status="consumed", consumed_at=now),
        )
        await session.execute(
            sa.update(oauth_clients).where(oauth_clients.c.id == client_id).values(last_used=now),
        )
        await session.commit()
    result = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "scope": " ".join(authority + ([OFFLINE_SCOPE] if refresh_token else [])),
    }
    if refresh_token:
        result["refresh_token"] = refresh_token
    return result


async def _insert_refresh_token(
    session,
    *,
    family_id: str,
    client_id: str,
    account_id,
    scopes: list[str],
    auth_method: str,
    authorized_at: datetime,
    now: datetime,
) -> str:
    """Insert one hash-only refresh token; the caller commits."""
    token = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(48)
    expires_at = min(
        now + timedelta(seconds=REFRESH_TOKEN_IDLE_SECONDS),
        _utc(authorized_at) + timedelta(seconds=REFRESH_SESSION_MAX_SECONDS),
    )
    await session.execute(
        sa.insert(oauth_refresh_tokens).values(
            token_hash=_hash_capability(token),
            family_id=family_id,
            client_id=client_id,
            account_id=account_id,
            resource=resource(),
            scopes=scopes,
            auth_method=auth_method,
            authorized_at=authorized_at,
            created=now,
            expires_at=expires_at,
        ),
    )
    return token


async def _revoke_family(session, family_id: str, *, reason: str, now: datetime) -> int:
    result = await session.execute(
        sa.update(oauth_refresh_tokens)
        .where(
            oauth_refresh_tokens.c.family_id == family_id,
            oauth_refresh_tokens.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoke_reason=reason),
    )
    return max(0, int(result.rowcount or 0))


async def refresh_access_token(
    *,
    refresh_token: str,
    client_id: str,
    requested_resource: str = "",
    scope: str = "",
) -> dict:
    """Rotate one refresh token and issue a new access token."""
    if not refresh_enabled():
        raise OAuthProtocolError("unsupported_grant_type", "Only authorization_code is supported")
    invalid = OAuthProtocolError("invalid_grant", "Invalid or expired refresh token")
    presented = str(refresh_token or "")
    if not presented.startswith(REFRESH_TOKEN_PREFIX) or len(presented) > 256:
        raise invalid
    if requested_resource and str(requested_resource).rstrip("/") != resource():
        raise OAuthProtocolError("invalid_target", "The requested resource does not match the authorization")
    now = datetime.now(UTC)

    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(oauth_refresh_tokens)
                    .where(oauth_refresh_tokens.c.token_hash == _hash_capability(presented))
                    .with_for_update(),
                )
            )
            .mappings()
            .first()
        )
        if not row or row["client_id"] != client_id or row["resource"] != resource():
            raise invalid
        if row["revoked_at"] is not None:
            raise invalid
        if row["used_at"] is not None:
            # A consumed token came back: the holder of its successor and this
            # caller cannot both be the client. End the session for both.
            await _revoke_family(session, row["family_id"], reason="reuse", now=now)
            await session.commit()
            raise invalid
        if _utc(row["expires_at"]) <= now:
            raise invalid
        client_active = await session.scalar(
            sa.select(oauth_clients.c.active).where(oauth_clients.c.id == client_id),
        )
        if not client_active:
            raise invalid

        granted = list(row["scopes"])
        authority = granted
        if scope:
            requested = set(str(scope).split()) - {OFFLINE_SCOPE}
            if not requested or not requested.issubset(granted):
                raise OAuthProtocolError("invalid_scope", "A refresh may only narrow the granted scopes")
            authority = sorted(requested)

        authorized_at = _utc(row["authorized_at"])
        access_token = user_tokens.issue(
            row["account_id"],
            audience=resource(),
            scopes=authority,
            auth_method=row["auth_method"] or "app",
            client_id=client_id,
            lifetime_seconds=ACCESS_TOKEN_TTL_SECONDS,
            auth_time=int(authorized_at.timestamp()),
        )
        await session.execute(
            sa.update(oauth_refresh_tokens)
            .where(oauth_refresh_tokens.c.token_hash == row["token_hash"])
            .values(used_at=now),
        )
        successor = await _insert_refresh_token(
            session,
            family_id=row["family_id"],
            client_id=client_id,
            account_id=row["account_id"],
            scopes=granted,
            auth_method=row["auth_method"] or "app",
            authorized_at=authorized_at,
            now=now,
        )
        await session.execute(
            sa.update(oauth_clients).where(oauth_clients.c.id == client_id).values(last_used=now),
        )
        await session.commit()
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "scope": " ".join(authority + [OFFLINE_SCOPE]),
        "refresh_token": successor,
    }


async def revoke_refresh_token(*, token: str, client_id: str) -> None:
    """RFC 7009: end the session a refresh token belongs to. Never reveals validity."""
    presented = str(token or "")
    if not presented.startswith(REFRESH_TOKEN_PREFIX) or len(presented) > 256:
        return
    now = datetime.now(UTC)
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(oauth_refresh_tokens.c.family_id, oauth_refresh_tokens.c.client_id).where(
                        oauth_refresh_tokens.c.token_hash == _hash_capability(presented),
                    ),
                )
            )
            .mappings()
            .first()
        )
        if not row or row["client_id"] != client_id:
            return
        await _revoke_family(session, row["family_id"], reason="client", now=now)
        await session.commit()


async def list_sessions(account_id) -> list[dict]:
    """Live refresh sessions of one account, newest first. Never returns tokens."""
    canonical_id = await identities.canonical_account_id(account_id)
    now = datetime.now(UTC)
    async with await new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(
                        oauth_refresh_tokens.c.family_id,
                        oauth_refresh_tokens.c.client_id,
                        oauth_refresh_tokens.c.scopes,
                        oauth_refresh_tokens.c.authorized_at,
                        oauth_refresh_tokens.c.created,
                        oauth_refresh_tokens.c.expires_at,
                        oauth_clients.c.name.label("client_name"),
                    )
                    .join(oauth_clients, oauth_clients.c.id == oauth_refresh_tokens.c.client_id)
                    .where(
                        oauth_refresh_tokens.c.account_id == canonical_id,
                        oauth_refresh_tokens.c.used_at.is_(None),
                        oauth_refresh_tokens.c.revoked_at.is_(None),
                        oauth_refresh_tokens.c.expires_at > now,
                    )
                    .order_by(oauth_refresh_tokens.c.created.desc())
                    .limit(100),
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "session_id": row["family_id"],
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "scopes": list(row["scopes"]),
            "authorized_at": _utc(row["authorized_at"]).isoformat(),
            "last_refreshed_at": _utc(row["created"]).isoformat(),
            "expires_at": _utc(row["expires_at"]).isoformat(),
        }
        for row in rows
    ]


async def revoke_session(account_id, session_id: str) -> bool:
    """End one of the account's own sessions. False when it is not theirs or not live."""
    canonical_id = await identities.canonical_account_id(account_id)
    now = datetime.now(UTC)
    async with await new_session() as session:
        owned = await session.scalar(
            sa.select(sa.func.count())
            .select_from(oauth_refresh_tokens)
            .where(
                oauth_refresh_tokens.c.family_id == str(session_id or ""),
                oauth_refresh_tokens.c.account_id == canonical_id,
            ),
        )
        if not owned:
            return False
        changed = await _revoke_family(session, str(session_id), reason="user", now=now)
        await session.commit()
    return changed > 0


def introspect_access_token(token: str) -> dict:
    if not enabled():
        return {"active": False}
    try:
        claims = user_tokens.verify(token, audience=resource())
    except HTTPException:
        return {"active": False}
    if (
        claims.get("service_id") is not None
        or not claims.get("client_id")
        or not set(claims.get("scopes") or []).issubset(ALLOWED_SCOPES)
    ):
        return {"active": False}
    return {
        "active": True,
        "client_id": claims["client_id"],
        "sub": claims["sub"],
        "aud": claims["aud"],
        "scope": " ".join(claims["scopes"]),
        "token_type": "Bearer",
        "iss": issuer(),
        "iat": claims["iat"],
        "exp": claims["exp"],
    }


async def prune_operational_state(
    *,
    authorization_retention_seconds: int = DEFAULT_AUTHORIZATION_RETENTION_SECONDS,
    unused_client_retention_seconds: int = DEFAULT_UNUSED_CLIENT_RETENTION_SECONDS,
    now: datetime | None = None,
) -> dict[str, int]:
    """Bound public registration and authorization state.

    Authorization rows are operational OAuth artifacts, not economic records.
    A one-day default exceeds the request, code, and access-token lifetimes by
    a wide margin while preventing unauthenticated registration traffic from
    growing the database forever. A client is removed only when it has never
    completed a token exchange and no retained authorization refers to it.
    """
    if authorization_retention_seconds < 3600:
        raise ValueError("authorization_retention_seconds must be at least 3600")
    if unused_client_retention_seconds < 3600:
        raise ValueError("unused_client_retention_seconds must be at least 3600")

    current = now or datetime.now(UTC)
    authorization_cutoff = current - timedelta(seconds=authorization_retention_seconds)
    client_cutoff = current - timedelta(seconds=unused_client_retention_seconds)
    retained_authorization = sa.exists(
        sa.select(oauth_authorizations.c.request_hash).where(
            oauth_authorizations.c.client_id == oauth_clients.c.id,
        ),
    )

    async with await new_session() as session:
        authorization_result = await session.execute(
            sa.delete(oauth_authorizations).where(
                oauth_authorizations.c.created < authorization_cutoff,
            ),
        )
        refresh_result = await session.execute(
            sa.delete(oauth_refresh_tokens).where(
                sa.or_(
                    oauth_refresh_tokens.c.expires_at < authorization_cutoff,
                    oauth_refresh_tokens.c.revoked_at < authorization_cutoff,
                    oauth_refresh_tokens.c.used_at < authorization_cutoff,
                ),
            ),
        )
        client_result = await session.execute(
            sa.delete(oauth_clients).where(
                oauth_clients.c.created < client_cutoff,
                oauth_clients.c.last_used.is_(None),
                ~retained_authorization,
            ),
        )
        await session.commit()
    return {
        "authorizations": max(0, int(authorization_result.rowcount or 0)),
        "clients": max(0, int(client_result.rowcount or 0)),
        "refresh_tokens": max(0, int(refresh_result.rowcount or 0)),
    }
