# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wire-boundary tests for the remote-MCP OAuth routes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from grid_api.routers import oauth
from grid_api.services import oauth_server

ROOT = Path(__file__).resolve().parents[3]


def _request(body: bytes, content_type: str) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", content_type.encode())],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_token_form_requires_urlencoded_and_rejects_duplicates():
    with pytest.raises(oauth_server.OAuthProtocolError, match="Content-Type"):
        await oauth._bounded_form(_request(b"{}", "application/json"))
    with pytest.raises(oauth_server.OAuthProtocolError, match="Duplicate"):
        await oauth._bounded_form(
            _request(b"code=one&code=two", "application/x-www-form-urlencoded; charset=utf-8"),
        )


@pytest.mark.asyncio
async def test_registration_body_is_bounded_before_model_validation():
    with pytest.raises(HTTPException) as exc:
        await oauth._bounded_registration(_request(b"{}", "text/plain"))
    assert exc.value.status_code == 415

    with pytest.raises(HTTPException) as exc:
        await oauth._bounded_registration(_request(b"x" * 32_769, "application/json"))
    assert exc.value.status_code == 413

    body = json.dumps(
        {
            "client_name": "Test MCP client",
            "application_type": "native",
            "redirect_uris": ["http://127.0.0.1/callback"],
        },
    ).encode()
    parsed = await oauth._bounded_registration(_request(body, "application/json"))
    assert parsed.client_name == "Test MCP client"


def test_discovery_is_dark_by_default(monkeypatch):
    monkeypatch.delenv("GRID_MCP_OAUTH_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        oauth_server.require_enabled()
    assert exc.value.status_code == 404


def test_production_nginx_preserves_exact_oauth_and_mcp_edge_routes():
    nginx = (ROOT / "deploy/nginx/aipg-api.conf").read_text()
    assert "location = /.well-known/oauth-protected-resource {" in nginx
    assert "location = /.well-known/oauth-authorization-server {" in nginx
    assert "include /etc/nginx/aipg-api.d/*.conf;" in nginx
    assert "location = /v1/oauth/introspect {" in nginx
    assert "return 404 '{\"detail\":\"Not Found\"}';" in nginx
    assert "location /.well-known/" not in nginx
    assert oauth.OAUTH_INTROSPECTION_RATE_LIMIT == "1200/minute"

    bootstrap = (ROOT / "deploy/bootstrap.sh").read_text()
    assert "install -d -o root -g root -m 0755 /etc/nginx/aipg-api.d" in bootstrap


@pytest.mark.asyncio
async def test_console_consent_requires_fresh_google_or_wallet_proof(monkeypatch):
    async def authenticate(*_args, **_kwargs):
        return {
            "key_kind": "delegated_user",
            "service_id": "grid-console",
            "token_claims": {"amr": "app", "auth_time": int(time.time())},
        }

    monkeypatch.setattr(oauth.accounts_svc, "authenticate", authenticate)
    with pytest.raises(HTTPException, match="Google or wallet proof"):
        await oauth._require_console_user("service-key", None, "user-token")

    async def wallet_authenticate(*_args, **_kwargs):
        return {
            "key_kind": "delegated_user",
            "service_id": "grid-console",
            "token_claims": {"amr": "siwe", "auth_time": int(time.time())},
        }

    monkeypatch.setattr(oauth.accounts_svc, "authenticate", wallet_authenticate)
    result = await oauth._require_console_user("service-key", None, "user-token")
    assert result["service_id"] == "grid-console"


@pytest.mark.asyncio
async def test_metadata_advertises_refresh_only_when_enabled(monkeypatch):
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "1")
    monkeypatch.delenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", raising=False)
    dark = await oauth.authorization_server_metadata()
    assert dark["grant_types_supported"] == ["authorization_code"]
    assert dark["scopes_supported"] == ["account.read", "inference.submit"]
    assert "revocation_endpoint" not in dark

    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    live = await oauth.authorization_server_metadata()
    assert live["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert live["scopes_supported"] == ["account.read", "inference.submit", "offline_access"]
    assert live["revocation_endpoint"].endswith("/v1/oauth/revoke")
    assert (await oauth.protected_resource_metadata())["scopes_supported"] == live["scopes_supported"]


@pytest.mark.asyncio
async def test_token_endpoint_dispatches_grants_and_never_caches(monkeypatch):
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "1")
    calls = []

    async def refresh(**kwargs):
        calls.append(("refresh", kwargs))
        return {"access_token": "a", "refresh_token": "r"}

    async def exchange(**kwargs):
        calls.append(("code", kwargs))
        raise oauth_server.OAuthProtocolError("unsupported_grant_type", "nope")

    monkeypatch.setattr(oauth_server, "refresh_access_token", refresh)
    monkeypatch.setattr(oauth_server, "exchange_authorization_code", exchange)
    form = "application/x-www-form-urlencoded"
    response = await oauth.token.__wrapped__(
        _request(b"grant_type=refresh_token&refresh_token=tok&client_id=c&scope=account.read", form),
    )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls == [("refresh", {"refresh_token": "tok", "client_id": "c", "requested_resource": "", "scope": "account.read"})]

    response = await oauth.token.__wrapped__(_request(b"grant_type=password&client_id=c", form))
    assert response.status_code == 400 and json.loads(response.body)["error"] == "unsupported_grant_type"
    assert calls[-1][0] == "code"


@pytest.mark.asyncio
async def test_revocation_answers_200_and_sessions_require_the_console(monkeypatch):
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "1")
    seen = []

    async def revoke(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(oauth_server, "revoke_refresh_token", revoke)
    response = await oauth.revoke.__wrapped__(
        _request(b"token=whatever&client_id=c", "application/x-www-form-urlencoded"),
    )
    assert response.status_code == 200 and json.loads(response.body) == {}
    assert seen == [{"token": "whatever", "client_id": "c"}]

    with pytest.raises(HTTPException) as exc:
        await oauth.list_sessions.__wrapped__(_request(b"", "text/plain"), None, None, None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await oauth.revoke_session.__wrapped__(
            _request(b"", "text/plain"), oauth.SessionRevocation(session_id="s" * 32), None, None, None,
        )
    assert exc.value.status_code == 401

    monkeypatch.delenv("GRID_MCP_OAUTH_ENABLED")
    with pytest.raises(HTTPException) as exc:
        await oauth.revoke.__wrapped__(_request(b"token=x", "application/x-www-form-urlencoded"))
    assert exc.value.status_code == 404
