# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Account reads must not turn inference credentials into identity/key discovery."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from grid_api.routers import accounts


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,scopes,is_session,full", [
    ("inference", ["account.read", "inference.submit"], False, False),
    ("service", ["account.read", "account.manage"], False, False),
    ("oauth", ["account.read"], False, False),
    ("user_token", ["account.read"], False, False),
    ("user_token", ["account.read", "account.manage"], False, True),
    ("delegated_user", ["account.read", "account.manage"], False, True),
    ("session", ["account.read", "account.manage"], True, True),
    ("session", ["account.read"], True, False),
])
async def test_account_read_projects_only_authorized_details(monkeypatch, kind, scopes, is_session, full):
    account_id = UUID("11111111-1111-4111-8111-111111111111")
    auth = AsyncMock(return_value={
        "source": "v2", "account_id": account_id, "username": "operator",
        "wallet": "", "key_kind": kind, "scopes": scopes, "is_session": is_session,
    })
    monkeypatch.setattr(accounts.accounts_svc, "authenticate", auth)
    identities = AsyncMock(return_value=[{
        "kind": "email", "display_hint": "private@example.invalid",
        "is_primary": True, "verified_at": "verified",
    }])
    monkeypatch.setattr(accounts.identities_svc, "list_identities", identities)
    result = Mock()
    result.mappings.return_value.all.return_value = [{
        "hash": "a" * 64, "label": "private-key-label", "created": None,
        "last_used": None, "revoked": False,
    }]

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(return_value=result))

    database = AsyncMock(side_effect=session)
    monkeypatch.setattr(accounts, "new_session", database)
    app = FastAPI()
    app.include_router(accounts.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/account", headers={"apikey": "test-key"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert auth.call_args.kwargs["required_scope"] == "account.read"
    identities.assert_awaited_once_with(account_id)
    body = response.json()
    assert body["account_id"] == str(account_id)
    assert body["identities"][0]["verified"] is True
    if full:
        database.assert_awaited_once()
        assert body["identities"][0]["display_hint"] == "private@example.invalid"
        assert body["keys"][0]["label"] == "private-key-label"
    else:
        database.assert_not_awaited()
        assert body["keys"] == []
        assert body["identities"][0]["display_hint"] == "Linked account"
        assert "private@example.invalid" not in response.text
        assert "private-key-label" not in response.text


@pytest.mark.asyncio
async def test_account_read_denied_before_identity_or_database_access(monkeypatch):
    monkeypatch.setattr(accounts.accounts_svc, "authenticate", AsyncMock(
        side_effect=HTTPException(403, "Token lacks account.read scope")))
    identities = AsyncMock()
    database = AsyncMock()
    monkeypatch.setattr(accounts.identities_svc, "list_identities", identities)
    monkeypatch.setattr(accounts, "new_session", database)
    with pytest.raises(HTTPException) as error:
        await accounts.get_account(apikey="rig-only-key", authorization=None)
    assert error.value.status_code == 403
    identities.assert_not_awaited()
    database.assert_not_awaited()
