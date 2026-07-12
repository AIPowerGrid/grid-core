# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trust-boundary tests for scoped, one-use frontend identity assertions."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import accounts, assertions, promotions
from grid_api.routers import accounts as accounts_router
from grid_api.v2.schema import account_identities, api_keys, metadata


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
async def test_valid_google_assertion_creates_keyless_canonical_user(assertion_db):
    bridge, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="google", subject="google-sub-123")

    user = await accounts.authenticate(bridge_key, token)

    assert user["asserted_provider"] == "google"
    assert str(user["bridge_account_id"]) == bridge["id"]
    assert str(user["account_id"]) != bridge["id"]
    assert user["is_session"] is False
    async with await database.new_session() as session:
        key_count = await session.scalar(sa.select(sa.func.count()).select_from(api_keys))
        identity = (await session.execute(
            sa.select(account_identities.c.kind, account_identities.c.verified_at)
            .where(account_identities.c.account_id == user["account_id"])
        )).first()
    assert key_count == 1  # bridge key only; no orphaned user credential
    assert identity.kind == "google" and identity.verified_at is not None


@pytest.mark.asyncio
async def test_assertion_is_single_use(assertion_db):
    _, bridge_key = assertion_db
    token = assertions.sign(bridge_key, provider="google", subject="replay-sub")
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
    destination, destination_key = await accounts.create_account(oauth_sub="link-google-sub")
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
async def test_bridge_google_and_wallet_proofs_merge_accounts(assertion_db):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    _, bridge_key = assertion_db
    wallet = Account.create()
    wallet_account, _ = await accounts.create_account(wallet=wallet.address)
    nonce = await accounts_router._nonce_issue()
    message = f"Link wallet to AIPG Grid identity\n\nNonce: {nonce}"
    signature = Account.sign_message(
        encode_defunct(text=message), wallet.key,
    ).signature.hex()
    google_assertion = assertions.sign(
        bridge_key, provider="google", subject="linked-google-sub",
    )
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    result = await accounts_router.link_wallet_from_assertion(
        request,
        accounts_router.WalletLinkForm(
            message=message, signature=signature, address=wallet.address,
        ),
        apikey=bridge_key,
        authorization=None,
        x_grid_user_assertion=google_assertion,
    )

    assert result["status"] == "merged"
    google_owner = await accounts_router.identities_svc.resolve_identity(
        "google", "linked-google-sub",
    )
    wallet_owner = await accounts_router.identities_svc.resolve_identity(
        "wallet", wallet.address,
    )
    assert google_owner == wallet_owner
    assert str(await accounts_router.identities_svc.canonical_account_id(wallet_account["id"])) == str(google_owner)
