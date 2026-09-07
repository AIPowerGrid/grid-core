# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real signed-token authorization, with account/report storage isolated."""

import secrets
import time
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grid_api import auth
from grid_api.ratelimit import limiter
from grid_api.routers import validator as router
from grid_api.services import accounts, service_auth, user_tokens, validators

PATH = "/v1/account/validator-scorecards"


@pytest.fixture
def reader(monkeypatch):
    aid = uuid4()
    calls = []
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", secrets.token_hex(32))
    monkeypatch.setattr(auth, "_API_KEY_SALT", secrets.token_hex(32))

    async def account_auth(account_id, *, scopes):
        assert str(account_id) == str(aid)
        return {"source": "v2", "account_id": aid, "wallet": "", "scopes": scopes}

    async def no_key(_key):
        return None

    async def service(client_id):
        assert client_id == "console-test"
        return {"id": client_id, "active": True}

    async def unregistered(**_kwargs):
        raise validators.RegistrationError("No active validator registration")

    async def report(**kwargs):
        calls.append(kwargs)
        return {"items": [], "count": 0, "economic_effect": "none"}

    monkeypatch.setattr(accounts, "_account_auth", account_auth)
    monkeypatch.setattr(accounts, "resolve_api_key", no_key)
    monkeypatch.setattr(service_auth, "get_client", service)
    monkeypatch.setattr(validators, "active_validator", unregistered)
    monkeypatch.setattr(validators, "scorecards", report)
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router.router)

    def token(scopes=None, method="google", **kwargs):
        return user_tokens.issue(
            aid,
            audience="console-test",
            service_id="console-test",
            scopes=["account.read"] if scopes is None else scopes,
            auth_method=method,
            **kwargs,
        )

    with TestClient(app) as client:
        yield client, token, calls


@pytest.mark.parametrize("method", ["google", "siwe", "service"])
def test_account_without_wallet_or_node_can_read_aggregates(reader, method):
    client, token, calls = reader
    response = client.get(PATH, headers={"Authorization": f"Bearer {token(method=method)}"})
    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0, "economic_effect": "none"}
    assert len(calls) == 1


@pytest.mark.parametrize("kind,status", [("absent", 401), ("invalid", 401), ("expired", 401), ("scope", 403)])
def test_report_rejects_unauthorized_credentials(reader, kind, status):
    client, token, calls = reader
    credentials = {
        "absent": "",
        "invalid": "gridu_invalid.invalid",
        "expired": token(now=int(time.time()) - 3601),
        "scope": token(scopes=["inference.submit"]),
    }
    response = client.get(PATH, headers={"apikey": credentials[kind]})
    assert response.status_code == status
    assert calls == []


def test_report_validates_and_forwards_only_bounded_filters(reader):
    client, token, calls = reader
    headers = {"apikey": token()}
    response = client.get(
        PATH,
        headers=headers,
        params={
            "limit": 5,
            "since_hours": 24,
            "authority": "authoritative",
            "worker_id": "synthetic-worker",
            "model": "synthetic-model",
        },
    )
    assert response.status_code == 200
    assert calls == [
        {"limit": 5, "since_hours": 24, "authority": "authoritative", "worker_id": "synthetic-worker", "model": "synthetic-model"},
    ]
    for query in ("limit=501", "since_hours=2161", "authority=invalid", "worker_id=" + "a" * 65):
        assert client.get(PATH + "?" + query, headers=headers).status_code == 422
    assert len(calls) == 1


def test_read_access_does_not_grant_validator_work_or_private_health(reader):
    client, token, calls = reader
    headers = {"apikey": token()}
    for path in ("/v1/validator/assignments", "/v1/validator/assignments/health", "/v1/validator/scorecards"):
        assert client.get(path, headers=headers).status_code == 403
    assert client.post("/v1/validator/probe/example", headers=headers).status_code == 403
    assert client.post("/v1/validator/attest", headers=headers, json={"payload": {}}).status_code == 403
    # Even a read-scoped token still cannot use the node-only routes without registration.
    headers = {"apikey": token(scopes=["validator.read"])}
    assert client.get("/v1/validator/scorecards", headers=headers).status_code == 403
    assert client.get("/v1/validator/assignments/health", headers=headers).status_code == 403
    assert calls == []
