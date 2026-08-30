# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security and lifecycle tests for the dark remote-MCP OAuth server."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import accounts, oauth_server, user_tokens
from grid_api.v2.schema import metadata, oauth_authorizations, oauth_clients


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _query_value(url: str, name: str) -> str:
    return parse_qs(urlsplit(url).query)[name][0]


@pytest_asyncio.fixture
async def oauth_db(monkeypatch):
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
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "1")
    monkeypatch.setenv("GRID_OAUTH_ISSUER", "https://api.example.test")
    monkeypatch.setenv("GRID_OAUTH_RESOURCE", "https://api.example.test")
    monkeypatch.setenv(
        "GRID_OAUTH_CONSOLE_AUTHORIZE_URL",
        "https://console.example.test/oauth/authorize",
    )
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    account, _ = await accounts.create_account(
        username="OAuth user",
        issue_initial_key=False,
        grant_verified_welcome=False,
    )
    try:
        yield account
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.mark.asyncio
async def test_registration_rejects_insecure_or_confidential_clients(oauth_db):
    with pytest.raises(ValueError, match="HTTP redirect_uri"):
        await oauth_server.register_client(
            {"client_name": "Bad web app", "application_type": "web", "redirect_uris": ["http://example.test/callback"]},
        )
    with pytest.raises(ValueError, match="public clients"):
        await oauth_server.register_client(
            {
                "client_name": "Confidential app",
                "application_type": "web",
                "redirect_uris": ["https://example.test/callback"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )


@pytest.mark.asyncio
async def test_mcp_service_can_be_provisioned_with_introspection_only(oauth_db):
    _, key = await accounts.create_service_client(
        "grid-mcp",
        "Grid remote MCP",
        scopes=["oauth.introspect"],
    )
    service = await accounts.authenticate(key, required_scope="oauth.introspect")
    assert service["scopes"] == ["oauth.introspect"]
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(key, required_scope="identity.exchange")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_native_loopback_pkce_code_is_single_use_and_never_stored_plaintext(oauth_db):
    client = await oauth_server.register_client(
        {
            "client_name": "Local MCP client",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/callback"],
        },
    )
    redirect_uri = "http://127.0.0.1:49152/callback"
    verifier = "v" * 43
    consent_url = await oauth_server.create_authorization_request(
        client_id=client["client_id"],
        redirect_uri=redirect_uri,
        response_type="code",
        scope="inference.submit account.read",
        state="state-from-client",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        requested_resource="https://api.example.test/",
    )
    capability = _query_value(consent_url, "request")
    inspected = await oauth_server.inspect_authorization_request(capability)
    assert inspected["redirect_host"] == "127.0.0.1"
    assert inspected["scopes"] == ["account.read", "inference.submit"]

    callback = await oauth_server.decide_authorization_request(
        capability,
        approve=True,
        account_id=oauth_db["id"],
        auth_method="google",
    )
    code = _query_value(callback, "code")
    assert _query_value(callback, "state") == "state-from-client"

    with pytest.raises(oauth_server.OAuthProtocolError, match="Invalid authorization code or verifier"):
        await oauth_server.exchange_authorization_code(
            grant_type="authorization_code",
            code=code,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            code_verifier="x" * 43,
            requested_resource="https://api.example.test",
        )

    token = await oauth_server.exchange_authorization_code(
        grant_type="authorization_code",
        code=code,
        client_id=client["client_id"],
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        requested_resource="https://api.example.test",
    )
    claims = user_tokens.verify(token["access_token"], audience="https://api.example.test")
    assert claims["client_id"] == client["client_id"]
    assert claims["service_id"] is None
    assert claims["scopes"] == ["account.read", "inference.submit"]
    authenticated = await accounts.authenticate(
        token["access_token"],
        required_scope="inference.submit",
    )
    assert str(authenticated["account_id"]) == oauth_db["id"]
    introspected = oauth_server.introspect_access_token(token["access_token"])
    assert introspected["active"] is True
    assert introspected["iss"] == "https://api.example.test"
    assert introspected["aud"] == "https://api.example.test"
    assert introspected["client_id"] == client["client_id"]

    with pytest.raises(oauth_server.OAuthProtocolError, match="Invalid or expired authorization code"):
        await oauth_server.exchange_authorization_code(
            grant_type="authorization_code",
            code=code,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            requested_resource="https://api.example.test",
        )

    async with await database.new_session() as session:
        stored = (await session.execute(sa.select(oauth_authorizations))).mappings().one()
    assert stored["request_hash"] == hashlib.sha256(capability.encode()).hexdigest()
    assert stored["code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert capability not in str(stored)
    assert code not in str(stored)
    assert stored["status"] == "consumed"


@pytest.mark.asyncio
async def test_denial_closes_request_without_issuing_code(oauth_db):
    client = await oauth_server.register_client(
        {
            "client_name": "Web MCP client",
            "application_type": "web",
            "redirect_uris": ["https://client.example.test/callback"],
        },
    )
    verifier = "z" * 43
    consent_url = await oauth_server.create_authorization_request(
        client_id=client["client_id"],
        redirect_uri="https://client.example.test/callback",
        response_type="code",
        scope="account.read",
        state="deny-state",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        requested_resource="https://api.example.test",
    )
    capability = _query_value(consent_url, "request")
    callback = await oauth_server.decide_authorization_request(
        capability,
        approve=False,
        account_id=oauth_db["id"],
        auth_method="siwe",
    )
    query = parse_qs(urlsplit(callback).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["deny-state"]
    assert "code" not in query
    with pytest.raises(HTTPException) as exc:
        await oauth_server.inspect_authorization_request(capability)
    assert exc.value.status_code == 409


def test_oauth_access_tokens_reject_wrong_audience_and_invalid_client(monkeypatch):
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    token = user_tokens.issue(
        "00000000-0000-0000-0000-000000000001",
        audience="https://api.example.test",
        scopes=["account.read"],
        auth_method="google",
        client_id="grid_oauth_test",
        now=100,
    )
    with pytest.raises(HTTPException, match="audience mismatch"):
        user_tokens.verify(token, audience="https://other.example.test", now=101)
    with pytest.raises(ValueError, match="client_id"):
        user_tokens.issue(
            "00000000-0000-0000-0000-000000000001",
            audience="https://api.example.test",
            scopes=["account.read"],
            auth_method="google",
            client_id="x" * 97,
        )


def test_disabled_oauth_invalidates_introspection_without_parsing_tokens(monkeypatch):
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "0")
    assert oauth_server.introspect_access_token("not-a-token") == {"active": False}


@pytest.mark.asyncio
async def test_oauth_audience_requires_client_binding_and_rejects_service_token(oauth_db):
    unbound = user_tokens.issue(
        oauth_db["id"],
        audience="https://api.example.test",
        scopes=["account.read"],
        auth_method="google",
    )
    with pytest.raises(HTTPException, match="missing its client binding"):
        await accounts.authenticate(unbound, required_scope="account.read")

    delegated = user_tokens.issue(
        oauth_db["id"],
        audience="https://api.example.test",
        scopes=["account.read"],
        auth_method="google",
        service_id="frontend",
        client_id="grid_oauth_confused_deputy",
    )
    assert oauth_server.introspect_access_token(delegated) == {"active": False}


@pytest.mark.asyncio
async def test_prune_bounds_old_authorizations_and_never_used_clients(oauth_db):
    stale_client = await oauth_server.register_client(
        {
            "client_name": "Abandoned MCP client",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/callback"],
        },
    )
    used_client = await oauth_server.register_client(
        {
            "client_name": "Previously used MCP client",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/used"],
        },
    )
    recent_client = await oauth_server.register_client(
        {
            "client_name": "Recent MCP client",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/recent"],
        },
    )
    verifier = "v" * 43
    consent_url = await oauth_server.create_authorization_request(
        client_id=stale_client["client_id"],
        redirect_uri="http://127.0.0.1:49152/callback",
        response_type="code",
        scope="account.read inference.submit",
        state="stale-state",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        requested_resource="https://api.example.test",
    )
    capability = _query_value(consent_url, "request")
    current = datetime.now(UTC)
    old_created = current - timedelta(days=2)
    async with await database.new_session() as session:
        await session.execute(
            sa.update(oauth_authorizations)
            .where(oauth_authorizations.c.request_hash == hashlib.sha256(capability.encode()).hexdigest())
            .values(created=old_created, expires_at=old_created + timedelta(minutes=10)),
        )
        await session.execute(
            sa.update(oauth_clients)
            .where(oauth_clients.c.id.in_([stale_client["client_id"], used_client["client_id"]]))
            .values(created=old_created),
        )
        await session.execute(
            sa.update(oauth_clients)
            .where(oauth_clients.c.id == used_client["client_id"])
            .values(last_used=old_created + timedelta(hours=1)),
        )
        await session.commit()

    deleted = await oauth_server.prune_operational_state(now=current)
    assert deleted == {"authorizations": 1, "clients": 1}

    async with await database.new_session() as session:
        remaining_clients = set((await session.execute(sa.select(oauth_clients.c.id))).scalars())
        remaining_authorizations = (await session.execute(sa.select(oauth_authorizations.c.request_hash))).scalars().all()
    assert remaining_clients == {used_client["client_id"], recent_client["client_id"]}
    assert remaining_authorizations == []


@pytest.mark.asyncio
async def test_prune_rejects_retention_shorter_than_one_hour(oauth_db):
    with pytest.raises(ValueError, match="at least 3600"):
        await oauth_server.prune_operational_state(authorization_retention_seconds=3599)
