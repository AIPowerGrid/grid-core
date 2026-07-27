# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import uuid

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException
from starlette.requests import Request

from grid_api.routers import accounts
from grid_api.services import user_tokens


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: redis)
    return redis


@pytest.mark.asyncio
async def test_siwe_challenge_binds_origin_wallet_chain_and_time(fake_redis):
    wallet = Account.create()
    response = await accounts.wallet_challenge(
        _request(),
        accounts.WalletChallengeForm(
            address=wallet.address,
            domain="console.aipowergrid.io",
            uri="https://console.aipowergrid.io/",
            chain_id=8453,
        ),
    )
    message = response["message"]
    assert message.startswith(
        f"console.aipowergrid.io wants you to sign in with your Ethereum account:\n{wallet.address}"
    )
    assert "URI: https://console.aipowergrid.io/" in message
    assert "Chain ID: 8453" in message
    assert f"Nonce: {response['nonce']}" in message
    assert "Issued At:" in message and "Expiration Time:" in message
    stored = json.loads(fake_redis.values[f"{accounts._NONCE_PREFIX}{response['nonce']}"])
    assert stored["message"] == message
    assert stored["address"] == wallet.address.lower()


@pytest.mark.asyncio
async def test_siwe_challenge_rejects_cross_domain_uri(fake_redis):
    wallet = Account.create()
    with pytest.raises(HTTPException) as exc:
        await accounts.wallet_challenge(
            _request(),
            accounts.WalletChallengeForm(
                address=wallet.address,
                domain="console.aipowergrid.io",
                uri="https://evil.example/",
            ),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_wallet_verify_requires_exact_single_use_siwe(fake_redis, monkeypatch):
    wallet = Account.create()
    response = await accounts.wallet_challenge(
        _request(),
        accounts.WalletChallengeForm(
            address=wallet.address,
            domain="console.aipowergrid.io",
            uri="https://console.aipowergrid.io/",
        ),
    )

    async def existing_account(_wallet):
        return {"id": uuid.uuid4(), "username": "wallet-user"}

    monkeypatch.setattr(accounts.accounts_svc, "get_account_by_wallet", existing_account)
    monkeypatch.setattr(user_tokens, "issue", lambda *_args, **_kwargs: "grid-token")

    signature = Account.sign_message(
        encode_defunct(text=response["message"]),
        wallet.key,
    ).signature.hex()
    bad = accounts.WalletVerifyForm(
        message=response["message"] + "\n",
        signature=signature,
        address=wallet.address,
    )
    with pytest.raises(HTTPException):
        await accounts.wallet_verify(_request(), bad)

    # A malformed verify attempt does not burn the user's challenge.
    good = accounts.WalletVerifyForm(
        message=response["message"],
        signature=signature,
        address=wallet.address,
    )
    verified = await accounts.wallet_verify(_request(), good)
    assert verified["access_token"] == "grid-token"
    assert verified["wallet"] == wallet.address.lower()

    with pytest.raises(HTTPException) as replay:
        await accounts.wallet_verify(_request(), good)
    assert replay.value.status_code == 401


@pytest.mark.asyncio
async def test_legacy_generic_signin_is_disabled_by_default(fake_redis, monkeypatch):
    monkeypatch.delenv("GRID_LEGACY_SIWE_VERIFY_ENABLED", raising=False)
    wallet = Account.create()
    nonce = await accounts._nonce_issue()
    message = f"Sign in to AIPG Grid\n\nNonce: {nonce}"
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    with pytest.raises(HTTPException) as exc:
        await accounts.wallet_verify(
            _request(),
            accounts.WalletVerifyForm(
                message=message,
                signature=signature,
                address=wallet.address,
            ),
        )
    assert exc.value.status_code == 401
    assert "Legacy wallet sign-in is disabled" in exc.value.detail
