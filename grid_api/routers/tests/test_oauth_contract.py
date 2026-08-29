# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wire-boundary tests for the remote-MCP OAuth routes."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from grid_api.routers import oauth
from grid_api.services import oauth_server


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
