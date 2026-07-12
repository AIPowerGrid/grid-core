# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trust-boundary tests for scoped, one-use frontend identity assertions."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from grid_api import database
from grid_api.routers import accounts as accounts_router
from grid_api.services import accounts, assertions, promotions, service_auth, user_tokens
from grid_api.v2.schema import metadata


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, _value, *, nx=False, ex=None):
        assert ex
        if nx and key in self.values:
            return False
        self.values[key] = _value
        return True

    async def getdel(self, key):
        return self.values.pop(key, None)


@pytest_asyncio.fixture
async def assertion_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    fake_redis = FakeRedis()
    from grid_api import redis_client

    monkeypatch.setattr(redis_client, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(promotions, "PROMO_ENABLED", False)
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    bridge, bridge_key = await accounts.create_account(
        username="art-bridge", key_label="art-bridge", is_session=False,
        scopes=["account.read", "inference.submit", "identity.assert"],
    )
    try:
        yield bridge, bridge_key
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.mark.asyncio
async def test_global_google_assertion_is_retired(assertion_db):
    _, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="google", subject="google-sub-123")
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(bridge_key, token)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_assertion_is_single_use(assertion_db):
    _, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="app", subject="replay-sub")
    await accounts.authenticate(bridge_key, token)
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(bridge_key, token)
    assert exc.value.status_code == 401
    assert "already used" in exc.value.detail


@pytest.mark.asyncio
async def test_tampered_assertion_is_rejected(assertion_db):
    _, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="google", subject="safe-sub")
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload[:-1]}A.{sig}"
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(bridge_key, tampered)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_unscoped_key_cannot_assert_users(assertion_db):
    _, bridge_key = assertion_db
    account, ordinary_key = await accounts.create_account(username="ordinary", is_session=False)
    token = assertions.sign(ordinary_key, provider="google", subject="victim-sub")
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(ordinary_key, token)
    assert exc.value.status_code == 403
    assert UUID(account["id"])


@pytest.mark.asyncio
async def test_bridge_cannot_submit_without_user_assertion(assertion_db):
    _, bridge_key = assertion_db
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(bridge_key, required_scope="inference.submit")
    assert exc.value.status_code == 401
    assert "requires a user assertion" in exc.value.detail


@pytest.mark.asyncio
async def test_required_inference_scope_is_enforced(assertion_db):
    _, no_inference_key = await accounts.create_account(
        username="assert-only", is_session=False, scopes=["identity.assert"],
    )
    token = assertions.sign(no_inference_key, provider="google", subject="scope-test")
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(
            no_inference_key, token, required_scope="inference.submit",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_malformed_wallet_assertion_is_rejected(assertion_db):
    _, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="wallet", subject="0xnot-a-wallet")
    with pytest.raises(HTTPException) as exc:
        await accounts.authenticate(bridge_key, token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wallet_link_requires_both_session_and_wallet_signature(assertion_db):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    wallet = Account.create()
    source, _ = await accounts.create_account(wallet=wallet.address)
    destination, _ = await accounts.create_account(oauth_sub="link-google-sub")
    destination_key = user_tokens.issue(
        destination["id"], audience="direct", scopes=accounts.SESSION_SCOPES,
        auth_method="google",
    )
    nonce = await accounts_router._nonce_issue()
    message = f"Link wallet to AIPG Grid account {destination['id']}\n\nNonce: {nonce}"
    signature = Account.sign_message(
        encode_defunct(text=message), wallet.key,
    ).signature.hex()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    result = await accounts_router.link_wallet(
        request,
        accounts_router.WalletLinkForm(
            message=message, signature=signature, address=wallet.address,
        ),
        apikey=destination_key,
        authorization=None,
    )

    assert result["status"] == "merged"
    assert result["account_id"] == destination["id"]
    assert await accounts.get_account_by_wallet(wallet.address) is not None
    assert str(await accounts_router.identities_svc.canonical_account_id(source["id"])) == destination["id"]


@pytest.mark.asyncio
async def test_wallet_link_requires_google_step_up_and_merges_accounts(assertion_db):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    service, service_key = await accounts.create_service_client(
        "gallery-test", "Gallery test", allowed_providers=["app"],
    )
    app_account, _ = await accounts.create_account(
        username="gallery user", issue_initial_key=False,
        identity_kind="app", identity_subject="gallery-test:user-1",
    )
    app_token = service_auth.issue_user_token(
        app_account["id"], service_id="gallery-test", auth_method="app",
    )
    google_token = service_auth.issue_user_token(
        app_account["id"], service_id="gallery-test", auth_method="google",
        account_manage=True,
    )
    wallet = Account.create()
    wallet_account, _ = await accounts.create_account(wallet=wallet.address)
    nonce = await accounts_router._nonce_issue()
    message = f"Link wallet to AIPG Grid identity\n\nNonce: {nonce}"
    signature = Account.sign_message(
        encode_defunct(text=message), wallet.key,
    ).signature.hex()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    with pytest.raises(HTTPException) as exc:
        await accounts_router.link_wallet_from_assertion(
            request,
            accounts_router.WalletLinkForm(
                message=message, signature=signature, address=wallet.address,
            ),
            apikey=service_key,
            authorization=None,
            x_grid_user_assertion=None,
            x_grid_user_token=app_token,
        )
    assert exc.value.status_code == 403

    result = await accounts_router.link_wallet_from_assertion(
        request,
        accounts_router.WalletLinkForm(
            message=message, signature=signature, address=wallet.address,
        ),
        apikey=service_key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=google_token,
    )

    assert result["status"] == "merged"
    app_owner = await accounts_router.identities_svc.resolve_identity(
        "app", "gallery-test:user-1",
    )
    wallet_owner = await accounts_router.identities_svc.resolve_identity(
        "wallet", wallet.address,
    )
    assert app_owner == wallet_owner
    assert str(await accounts_router.identities_svc.canonical_account_id(wallet_account["id"])) == str(app_owner)


@pytest.mark.asyncio
async def test_app_assertion_creates_stable_non_google_identity(assertion_db):
    _, bridge_key = assertion_db
    first = assertions.sign(
        bridge_key, provider="app", subject="aipg-chat:user-123",
    )
    user = await accounts.authenticate(
        bridge_key, first, required_scope="inference.submit",
    )
    bridge, _ = assertion_db
    owner = await accounts_router.identities_svc.resolve_identity(
        "app", f"{bridge['id']}:aipg-chat:user-123",
    )
    assert owner == user["account_id"]
    assert user["asserted_provider"] == "app"

    second = assertions.sign(
        bridge_key, provider="app", subject="aipg-chat:user-123",
    )
    same_user = await accounts.authenticate(
        bridge_key, second, required_scope="account.read",
    )
    assert same_user["account_id"] == user["account_id"]
