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
from grid_api.v2.schema import metadata, oauth_authorizations, oauth_clients, oauth_refresh_tokens


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
    assert deleted == {"authorizations": 1, "clients": 1, "refresh_tokens": 0}

    async with await database.new_session() as session:
        remaining_clients = set((await session.execute(sa.select(oauth_clients.c.id))).scalars())
        remaining_authorizations = (await session.execute(sa.select(oauth_authorizations.c.request_hash))).scalars().all()
    assert remaining_clients == {used_client["client_id"], recent_client["client_id"]}
    assert remaining_authorizations == []


@pytest.mark.asyncio
async def test_prune_rejects_retention_shorter_than_one_hour(oauth_db):
    with pytest.raises(ValueError, match="at least 3600"):
        await oauth_server.prune_operational_state(authorization_retention_seconds=3599)


# ── Refresh tokens ───────────────────────────────────────────────────────

RESOURCE = "https://api.example.test"


async def _authorize(account_id, *, scope="inference.submit account.read offline_access", auth_method="google"):
    """Register a native client, approve it and exchange the code."""
    client = await oauth_server.register_client(
        {
            "client_name": "Desktop app",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"]
            if oauth_server.refresh_enabled()
            else ["authorization_code"],
        },
    )
    redirect_uri = "http://127.0.0.1:50000/callback"
    verifier = "r" * 43
    consent = await oauth_server.create_authorization_request(
        client_id=client["client_id"],
        redirect_uri=redirect_uri,
        response_type="code",
        scope=scope,
        state="s",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        requested_resource=RESOURCE,
    )
    callback = await oauth_server.decide_authorization_request(
        _query_value(consent, "request"), approve=True, account_id=account_id, auth_method=auth_method,
    )
    tokens = await oauth_server.exchange_authorization_code(
        grant_type="authorization_code",
        code=_query_value(callback, "code"),
        client_id=client["client_id"],
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        requested_resource=RESOURCE,
    )
    return client, tokens


async def _rows():
    async with await database.new_session() as session:
        return (await session.execute(sa.select(oauth_refresh_tokens).order_by(oauth_refresh_tokens.c.created))).mappings().all()


@pytest.mark.asyncio
async def test_refresh_is_dark_by_default(oauth_db):
    assert oauth_server.refresh_enabled() is False
    assert oauth_server.supported_grant_types() == ["authorization_code"]
    assert "offline_access" not in oauth_server.supported_scopes()
    with pytest.raises(ValueError, match="authorization_code grant is supported"):
        await oauth_server.register_client(
            {"redirect_uris": ["http://127.0.0.1/callback"], "grant_types": ["authorization_code", "refresh_token"]},
        )
    with pytest.raises(oauth_server.OAuthProtocolError) as error:
        await _authorize(oauth_db["id"])
    assert error.value.error == "invalid_scope"
    client, tokens = await _authorize(oauth_db["id"], scope="inference.submit")
    assert "refresh_token" not in tokens
    with pytest.raises(oauth_server.OAuthProtocolError) as error:
        await oauth_server.refresh_access_token(refresh_token="oauth_refresh_x", client_id=client["client_id"])
    assert error.value.error == "unsupported_grant_type"
    assert await _rows() == []


@pytest.mark.asyncio
async def test_refresh_requires_the_flag_and_mcp_oauth_together(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    assert oauth_server.refresh_enabled() is True
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "0")
    assert oauth_server.refresh_enabled() is False


@pytest.mark.asyncio
async def test_offline_access_must_be_requested_and_is_not_an_authority(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    _, plain = await _authorize(oauth_db["id"], scope="inference.submit")
    assert "refresh_token" not in plain and plain["scope"] == "inference.submit"
    with pytest.raises(oauth_server.OAuthProtocolError) as error:
        await _authorize(oauth_db["id"], scope="offline_access")
    assert error.value.error == "invalid_scope"

    _, tokens = await _authorize(oauth_db["id"])
    assert tokens["scope"] == "account.read inference.submit offline_access"
    claims = user_tokens.verify(tokens["access_token"], audience=RESOURCE)
    assert claims["scopes"] == ["account.read", "inference.submit"]
    assert oauth_server.introspect_access_token(tokens["access_token"])["active"] is True
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
    assert tokens["refresh_token"] not in str(dict(rows[0]))
    assert rows[0]["scopes"] == ["account.read", "inference.submit"]


@pytest.mark.asyncio
async def test_refresh_rotates_and_preserves_the_original_proof_time(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    # Age the session: the consent happened an hour ago.
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    async with await database.new_session() as session:
        await session.execute(sa.update(oauth_refresh_tokens).values(authorized_at=hour_ago))
        await session.commit()

    refreshed = await oauth_server.refresh_access_token(
        refresh_token=tokens["refresh_token"], client_id=client["client_id"], requested_resource=RESOURCE + "/",
    )
    assert refreshed["refresh_token"] != tokens["refresh_token"]
    assert refreshed["scope"] == "account.read inference.submit offline_access"
    claims = user_tokens.verify(refreshed["access_token"], audience=RESOURCE)
    assert claims["client_id"] == client["client_id"] and claims["amr"] == "google"
    # A refresh is not a new proof of identity: the original consent time stays.
    assert abs(claims["auth_time"] - int(hour_ago.timestamp())) <= 1
    with pytest.raises(HTTPException) as stale:
        user_tokens.require_recent_step_up({**claims, "client_id": None})
    assert stale.value.status_code == 403
    authenticated = await accounts.authenticate(refreshed["access_token"], required_scope="inference.submit")
    assert str(authenticated["account_id"]) == oauth_db["id"]

    rows = await _rows()
    assert len(rows) == 2 and len({row["family_id"] for row in rows}) == 1
    assert sum(row["used_at"] is not None for row in rows) == 1
    second = await oauth_server.refresh_access_token(refresh_token=refreshed["refresh_token"], client_id=client["client_id"])
    assert second["refresh_token"] not in {tokens["refresh_token"], refreshed["refresh_token"]}


@pytest.mark.asyncio
async def test_replaying_a_consumed_refresh_token_revokes_the_session(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    other_client, other = await _authorize(oauth_db["id"])
    refreshed = await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])

    with pytest.raises(oauth_server.OAuthProtocolError) as replay:
        await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    assert replay.value.error == "invalid_grant"
    # The legitimate successor dies with it.
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=refreshed["refresh_token"], client_id=client["client_id"])
    family = [row for row in await _rows() if row["client_id"] == client["client_id"]]
    assert family and all(row["revoke_reason"] == "reuse" for row in family)
    # Another session of the same account is untouched.
    assert (await oauth_server.refresh_access_token(refresh_token=other["refresh_token"], client_id=other_client["client_id"]))["access_token"]


@pytest.mark.asyncio
async def test_refresh_rejects_wrong_client_resource_scope_expiry_and_inactive_client(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"], scope="inference.submit offline_access")
    other_client, _ = await _authorize(oauth_db["id"])
    token = tokens["refresh_token"]

    for kwargs, error in [
        ({"refresh_token": token, "client_id": other_client["client_id"]}, "invalid_grant"),
        ({"refresh_token": token, "client_id": client["client_id"], "requested_resource": "https://evil.example"}, "invalid_target"),
        ({"refresh_token": token, "client_id": client["client_id"], "scope": "account.read"}, "invalid_scope"),
        ({"refresh_token": token, "client_id": client["client_id"], "scope": "offline_access"}, "invalid_scope"),
        ({"refresh_token": "oauth_refresh_" + "z" * 64, "client_id": client["client_id"]}, "invalid_grant"),
        ({"refresh_token": "", "client_id": client["client_id"]}, "invalid_grant"),
        ({"refresh_token": "gridu_not-a-refresh-token", "client_id": client["client_id"]}, "invalid_grant"),
    ]:
        with pytest.raises(oauth_server.OAuthProtocolError) as raised:
            await oauth_server.refresh_access_token(**kwargs)
        assert raised.value.error == error, kwargs
    # None of those rejections consumed or revoked the real token.
    mine = [row for row in await _rows() if row["client_id"] == client["client_id"]]
    assert len(mine) == 1 and mine[0]["used_at"] is None and mine[0]["revoked_at"] is None

    async with await database.new_session() as session:
        await session.execute(sa.update(oauth_clients).where(oauth_clients.c.id == client["client_id"]).values(active=False))
        await session.commit()
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=token, client_id=client["client_id"])
    async with await database.new_session() as session:
        await session.execute(sa.update(oauth_clients).values(active=True))
        past = datetime.now(UTC) - timedelta(days=2)
        await session.execute(
            sa.update(oauth_refresh_tokens)
            .where(oauth_refresh_tokens.c.client_id == client["client_id"])
            .values(created=past - timedelta(days=1), expires_at=past),
        )
        await session.commit()
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=token, client_id=client["client_id"])


@pytest.mark.asyncio
async def test_refresh_can_narrow_scopes_without_narrowing_the_session(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    narrowed = await oauth_server.refresh_access_token(
        refresh_token=tokens["refresh_token"], client_id=client["client_id"], scope="account.read",
    )
    assert user_tokens.verify(narrowed["access_token"], audience=RESOURCE)["scopes"] == ["account.read"]
    full = await oauth_server.refresh_access_token(refresh_token=narrowed["refresh_token"], client_id=client["client_id"])
    assert user_tokens.verify(full["access_token"], audience=RESOURCE)["scopes"] == ["account.read", "inference.submit"]


@pytest.mark.asyncio
async def test_session_lifetime_is_capped_from_the_original_consent(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    consent = datetime.now(UTC) - timedelta(seconds=oauth_server.REFRESH_SESSION_MAX_SECONDS - 3600)
    async with await database.new_session() as session:
        await session.execute(sa.update(oauth_refresh_tokens).values(authorized_at=consent))
        await session.commit()
    refreshed = await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    newest = [row for row in await _rows() if row["used_at"] is None][0]
    remaining = oauth_server._utc(newest["expires_at"]) - datetime.now(UTC)
    assert timedelta(minutes=55) < remaining <= timedelta(hours=1)
    assert refreshed["refresh_token"]


@pytest.mark.asyncio
async def test_client_revocation_ends_only_its_own_session_and_reveals_nothing(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    other_client, other = await _authorize(oauth_db["id"])
    # Wrong client, unknown and malformed tokens are silent no-ops.
    assert await oauth_server.revoke_refresh_token(token=tokens["refresh_token"], client_id=other_client["client_id"]) is None
    assert await oauth_server.revoke_refresh_token(token="oauth_refresh_unknown", client_id=client["client_id"]) is None
    assert await oauth_server.revoke_refresh_token(token="", client_id=client["client_id"]) is None
    assert all(row["revoked_at"] is None for row in await _rows())

    await oauth_server.revoke_refresh_token(token=tokens["refresh_token"], client_id=client["client_id"])
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    assert (await oauth_server.refresh_access_token(refresh_token=other["refresh_token"], client_id=other_client["client_id"]))["access_token"]
    reasons = {row["client_id"]: row["revoke_reason"] for row in await _rows() if row["revoked_at"] is not None}
    assert reasons == {client["client_id"]: "client"}


@pytest.mark.asyncio
async def test_account_can_list_and_revoke_only_its_own_sessions(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    stranger, _ = await accounts.create_account(username="Stranger", issue_initial_key=False, grant_verified_welcome=False)
    client, tokens = await _authorize(oauth_db["id"])
    await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    _, theirs = await _authorize(stranger["id"])

    sessions = await oauth_server.list_sessions(oauth_db["id"])
    assert len(sessions) == 1  # one session, however many times it rotated
    listed = sessions[0]
    assert listed["client_name"] == "Desktop app" and listed["scopes"] == ["account.read", "inference.submit"]
    assert "oauth_refresh_" not in str(sessions) and "token_hash" not in listed

    their_session = (await oauth_server.list_sessions(stranger["id"]))[0]["session_id"]
    assert await oauth_server.revoke_session(oauth_db["id"], their_session) is False
    assert await oauth_server.revoke_session(oauth_db["id"], "no-such-session-id") is False
    assert len(await oauth_server.list_sessions(stranger["id"])) == 1

    assert await oauth_server.revoke_session(oauth_db["id"], listed["session_id"]) is True
    assert await oauth_server.list_sessions(oauth_db["id"]) == []
    assert await oauth_server.revoke_session(oauth_db["id"], listed["session_id"]) is False
    assert theirs["refresh_token"]


@pytest.mark.asyncio
async def test_prune_keeps_replay_evidence_while_a_session_is_live(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, stolen = await _authorize(oauth_db["id"])
    # A thief uses a copy first; the real client is idle for longer than the sweeper's retention.
    thief = await oauth_server.refresh_access_token(refresh_token=stolen["refresh_token"], client_id=client["client_id"])
    later = datetime.now(UTC) + timedelta(days=3)
    deleted = await oauth_server.prune_operational_state(now=later)
    assert deleted["refresh_tokens"] == 0 and len(await _rows()) == 2

    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=stolen["refresh_token"], client_id=client["client_id"])
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=thief["refresh_token"], client_id=client["client_id"])
    assert {row["revoke_reason"] for row in await _rows()} == {"reuse"}


@pytest.mark.asyncio
async def test_prune_removes_a_whole_session_only_after_it_ended(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    keep_client, keep = await _authorize(oauth_db["id"])
    sessions = await oauth_server.list_sessions(oauth_db["id"])
    ended = next(item["session_id"] for item in sessions if item["client_id"] == client["client_id"])
    assert await oauth_server.revoke_session(oauth_db["id"], ended) is True

    soon = await oauth_server.prune_operational_state(now=datetime.now(UTC) + timedelta(hours=1))
    assert soon["refresh_tokens"] == 0  # still inside the retention period
    deleted = await oauth_server.prune_operational_state(now=datetime.now(UTC) + timedelta(days=2))
    assert deleted["refresh_tokens"] == 2 and deleted["clients"] == 0
    rows = await _rows()
    assert [row["client_id"] for row in rows] == [keep_client["client_id"]]
    assert (await oauth_server.refresh_access_token(refresh_token=keep["refresh_token"], client_id=keep_client["client_id"]))["access_token"]

    # An expired, never-revoked session is removed as a whole too.
    past = datetime.now(UTC) - timedelta(days=40)
    async with await database.new_session() as session:
        await session.execute(sa.update(oauth_refresh_tokens).values(created=past, expires_at=past + timedelta(days=30)))
        await session.commit()
    assert (await oauth_server.prune_operational_state())["refresh_tokens"] == 2
    assert await _rows() == []


@pytest.mark.asyncio
async def test_merging_an_account_ends_its_sessions_and_stragglers_stay_visible(oauth_db, monkeypatch):
    from grid_api.services import identities

    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    retired, _ = await accounts.create_account(username="Old login", issue_initial_key=False, grant_verified_welcome=False)
    client, tokens = await _authorize(retired["id"])
    kept_client, kept = await _authorize(oauth_db["id"])

    await identities.merge_accounts(oauth_db["id"], retired["id"], merge_ref="merge-test")
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    reasons = {row["client_id"]: row["revoke_reason"] for row in await _rows()}
    assert reasons == {client["client_id"]: "merge", kept_client["client_id"]: None}
    assert (await oauth_server.refresh_access_token(refresh_token=kept["refresh_token"], client_id=kept_client["client_id"]))["access_token"]

    # A session that lands on the retired account after the merge authenticates
    # as the surviving account, so that account can see it and end it.
    async with await database.new_session() as session:
        straggler = await oauth_server._insert_refresh_token(
            session, family_id="straggler-family-0001", client_id=client["client_id"],
            account_id=__import__("uuid").UUID(str(retired["id"])), scopes=["account.read"], auth_method="google",
            authorized_at=datetime.now(UTC), now=datetime.now(UTC),
        )
        await session.commit()
    listed = {item["session_id"] for item in await oauth_server.list_sessions(oauth_db["id"])}
    assert "straggler-family-0001" in listed
    assert await oauth_server.revoke_session(oauth_db["id"], "straggler-family-0001") is True
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=straggler, client_id=client["client_id"])


@pytest.mark.asyncio
async def test_a_token_in_a_partly_revoked_session_is_dead(oauth_db, monkeypatch):
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    client, tokens = await _authorize(oauth_db["id"])
    refreshed = await oauth_server.refresh_access_token(refresh_token=tokens["refresh_token"], client_id=client["client_id"])
    # Simulate a revocation that reached only the predecessor.
    async with await database.new_session() as session:
        await session.execute(
            sa.update(oauth_refresh_tokens)
            .where(oauth_refresh_tokens.c.used_at.is_not(None))
            .values(revoked_at=datetime.now(UTC), revoke_reason="user"),
        )
        await session.commit()
    assert await oauth_server.list_sessions(oauth_db["id"]) != []  # the row itself still looks live
    with pytest.raises(oauth_server.OAuthProtocolError):
        await oauth_server.refresh_access_token(refresh_token=refreshed["refresh_token"], client_id=client["client_id"])
    assert {row["revoke_reason"] for row in await _rows()} == {"user"}
    assert await oauth_server.list_sessions(oauth_db["id"]) == []


def test_user_token_auth_time_cannot_be_in_the_future(monkeypatch):
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    with pytest.raises(ValueError):
        user_tokens.issue("a", audience="x", scopes=["account.read"], auth_method="google", now=1000, auth_time=1001)
    token = user_tokens.issue("a", audience="x", scopes=["account.read"], auth_method="google", now=5000, auth_time=1000)
    assert user_tokens.verify(token, now=5001)["auth_time"] == 1000


@pytest.mark.asyncio
async def test_oauth_application_token_can_never_satisfy_account_step_up(oauth_db):
    """A third-party OAuth token is fresh and carries the user's proof method,
    but it speaks for an application. It must not pass account-management gates."""
    from starlette.requests import Request

    from grid_api.routers import accounts as accounts_router

    client = await oauth_server.register_client(
        {"client_name": "Third-party app", "application_type": "native", "redirect_uris": ["http://127.0.0.1/callback"]},
    )
    verifier = "s" * 43
    consent = await oauth_server.create_authorization_request(
        client_id=client["client_id"],
        redirect_uri="http://127.0.0.1:50001/callback",
        response_type="code",
        scope="account.read",
        state="s",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
        requested_resource="https://api.example.test",
    )
    callback = await oauth_server.decide_authorization_request(
        _query_value(consent, "request"), approve=True, account_id=oauth_db["id"], auth_method="google",
    )
    token = (
        await oauth_server.exchange_authorization_code(
            grant_type="authorization_code",
            code=_query_value(callback, "code"),
            client_id=client["client_id"],
            redirect_uri="http://127.0.0.1:50001/callback",
            code_verifier=verifier,
            requested_resource="https://api.example.test",
        )
    )["access_token"]
    claims = user_tokens.verify(token)
    assert claims["amr"] == "google" and claims["client_id"] == client["client_id"]

    with pytest.raises(HTTPException) as gate:
        user_tokens.require_recent_step_up(claims)
    assert gate.value.status_code == 403

    # The same account's own first-party token still passes.
    own = user_tokens.verify(
        user_tokens.issue(oauth_db["id"], audience="grid-console", scopes=["account.read"], auth_method="google"),
    )
    user_tokens.require_recent_step_up(own)

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
    form = accounts_router.WalletLinkForm(
        address="0x" + "1" * 40,
        message="Link wallet to AIPG Grid identity\n\nNonce: abcdef",
        signature="0x" + "0" * 130,
    )
    with pytest.raises(HTTPException) as route:
        await accounts_router.link_wallet_from_assertion.__wrapped__(
            request, form, None, f"Bearer {token}", None, "any-header-value",
        )
    assert route.value.status_code == 403
