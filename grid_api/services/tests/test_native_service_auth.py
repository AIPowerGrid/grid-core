# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from grid_api import database
from grid_api.routers import accounts as accounts_router
from grid_api.services import (
    accounts,
    alerts,
    credits,
    identities,
    quota,
    service_auth,
    service_limits,
    user_tokens,
)
from grid_api.v2.schema import accounts as accounts_table
from grid_api.v2.schema import metadata
from grid_api.v2.schema import service_clients as service_clients_table


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.mark.asyncio
async def test_service_exchange_is_namespaced_and_short_lived(db):
    service, key = await accounts.create_service_client(
        "gallery-test",
        "Gallery",
        allowed_providers=["app"],
        per_request_micro=500_000,
        daily_micro=2_000_000,
    )
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    result = await accounts_router.exchange_service_identity(
        request,
        accounts_router.ServiceExchangeForm(subject="local-user-1"),
        apikey=key,
        authorization=None,
    )
    delegated = await accounts.authenticate(
        key,
        user_token=result["access_token"],
        required_scope="inference.submit",
    )
    assert delegated["service_id"] == service["id"]
    assert delegated["key_kind"] == "delegated_user"
    assert delegated["service_limits"]["daily_micro"] == 2_000_000
    assert "account.manage" not in delegated["scopes"]

    same = await accounts_router.exchange_service_identity(
        request,
        accounts_router.ServiceExchangeForm(subject="local-user-1"),
        apikey=key,
        authorization=None,
    )
    assert same["account_id"] == result["account_id"]


@pytest.mark.asyncio
async def test_native_exchange_absorbs_legacy_service_account_namespace(db):
    service, key = await accounts.create_service_client(
        "chat-legacy",
        "Chat",
        allowed_providers=["app"],
    )
    subject = "aipg-chat:user-1"
    legacy_subject = f"{service['account_id']}:{subject}"
    legacy_account, _ = await accounts.create_account(
        username="Legacy Chat user",
        issue_initial_key=False,
        identity_kind="app",
        identity_subject=legacy_subject,
    )
    assert await credits.credit(
        UUID(legacy_account["id"]),
        750_000,
        "test_funding",
        "test:legacy-service-namespace",
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
    )

    result = await accounts_router.exchange_service_identity(
        request,
        accounts_router.ServiceExchangeForm(subject=subject),
        apikey=key,
        authorization=None,
    )

    assert result["account_id"] == legacy_account["id"]
    assert str(
        await identities.resolve_identity("app", f"chat-legacy:{subject}"),
    ) == legacy_account["id"]
    assert await credits.get_balance(UUID(legacy_account["id"])) == 750_000


@pytest.mark.asyncio
async def test_verified_google_account_and_balance_are_shared_across_products(
    db,
    monkeypatch,
):
    async def verified_google(_id_token, audiences):
        assert audiences == ["shared-google-client"]
        return {
            "subject": "google-user-123",
            "email": "verified@example.test",
            "email_verified": True,
            "name": "Verified User",
        }

    async def no_campaign(*_args, **_kwargs):
        return None

    async def no_grant(*_args, **_kwargs):
        return {"status": "disabled"}

    async def no_value(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(service_auth, "verify_google_id_token", verified_google)
    monkeypatch.setattr(
        "grid_api.services.promotions.ensure_builtin_campaign",
        no_campaign,
    )
    monkeypatch.setattr("grid_api.services.promotions.grant_once", no_grant)
    monkeypatch.setattr("grid_api.services.promotions.available_micro", no_value)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", no_value)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", no_value)

    services = {}
    for service_id in ("aipg-art", "aipg-chat", "aipg-music"):
        services[service_id] = await accounts.create_service_client(
            service_id,
            service_id,
            allowed_providers=["app", "google", "wallet"],
            google_audiences=["shared-google-client"],
            siwe_domains=[f"{service_id}.test"],
        )

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
    )
    account_id = None
    for index, (service_id, (service, key)) in enumerate(services.items()):
        result = await accounts_router.exchange_google_identity(
            request,
            accounts_router.GoogleExchangeForm(
                id_token=f"google-proof-{index}",
                app_subject=f"{service_id}:local-user-{index}",
            ),
            apikey=key,
            authorization=None,
        )
        if account_id is None:
            account_id = result["account_id"]
            assert await credits.credit(
                UUID(account_id),
                20_000,
                "test_funding",
                "test:universal-product-balance",
            )
        assert result["account_id"] == account_id
        delegated = await accounts.authenticate(
            key,
            user_token=result["access_token"],
            required_scope="inference.submit",
        )
        assert str(delegated["account_id"]) == account_id
        assert delegated["service_id"] == service["id"]
        assert str(
            await identities.resolve_identity(
                "app",
                f"{service['id']}:{service_id}:local-user-{index}",
            ),
        ) == account_id
        credit_view = await accounts_router.get_credits(
            apikey=key,
            authorization=None,
            x_grid_user_assertion=None,
            x_grid_user_token=result["access_token"],
        )
        assert credit_view["account_id"] == account_id
        assert credit_view["paid"]["balance_micro"] == 20_000


class _NonceRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)


@pytest.mark.asyncio
async def test_verified_wallet_account_and_balance_are_shared_across_products(
    db,
    monkeypatch,
):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    redis = _NonceRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: redis)

    async def no_value(*_args, **_kwargs):
        return 0

    monkeypatch.setattr("grid_api.services.promotions.available_micro", no_value)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", no_value)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", no_value)
    services = {}
    for service_id, domain in (
        ("aipg-art", "aipg.art"),
        ("aipg-chat", "aipg.chat"),
        ("aipg-music", "aipg.music"),
    ):
        services[service_id] = await accounts.create_service_client(
            service_id,
            service_id,
            allowed_providers=["app", "wallet"],
            siwe_domains=[domain],
        )

    wallet = Account.create()
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
    )
    account_id = None
    for index, (service_id, (_service, key)) in enumerate(services.items()):
        domain = {
            "aipg-art": "aipg.art",
            "aipg-chat": "aipg.chat",
            "aipg-music": "aipg.music",
        }[service_id]
        app_subject = f"local-wallet-user-{index}"
        challenge = await accounts_router.exchange_wallet_challenge(
            request,
            accounts_router.ServiceWalletChallengeForm(
                address=wallet.address,
                domain=domain,
                uri=f"https://{domain}",
                app_subject=app_subject,
            ),
            apikey=key,
            authorization=None,
        )
        signature = Account.sign_message(
            encode_defunct(text=challenge["message"]),
            wallet.key,
        ).signature.hex()
        result = await accounts_router.exchange_wallet_identity(
            request,
            accounts_router.ServiceWalletExchangeForm(
                message=challenge["message"],
                signature=signature,
                address=wallet.address,
                app_subject=app_subject,
            ),
            apikey=key,
            authorization=None,
        )
        if account_id is None:
            account_id = result["account_id"]
            assert await credits.credit(
                UUID(account_id),
                20_000,
                "test_funding",
                "test:universal-wallet-balance",
            )
        assert result["account_id"] == account_id
        assert str(
            await identities.resolve_identity(
                "app",
                f"{service_id}:{app_subject}",
            ),
        ) == account_id
        credit_view = await accounts_router.get_credits(
            apikey=key,
            authorization=None,
            x_grid_user_assertion=None,
            x_grid_user_token=result["access_token"],
        )
        assert credit_view["account_id"] == account_id
        assert credit_view["paid"]["balance_micro"] == 20_000


@pytest.mark.asyncio
async def test_service_siwe_merges_funded_wallet_into_local_identity(
    db,
    monkeypatch,
):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    redis = _NonceRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: redis)
    service, key = await accounts.create_service_client(
        "gallery-test",
        "Gallery",
        allowed_providers=["app", "wallet"],
        siwe_domains=["aipg.art"],
        per_request_micro=500_000,
        daily_micro=2_000_000,
    )
    local_account, _ = await accounts.create_account(
        username="Gallery user",
        issue_initial_key=False,
        identity_kind="app",
        identity_subject="gallery-test:user-1",
    )
    wallet = Account.create()
    funded_account, _ = await accounts.create_account(
        wallet=wallet.address,
        issue_initial_key=False,
    )
    assert await credits.credit(
        UUID(funded_account["id"]),
        2_000_000,
        "test_funding",
        "test:service-siwe-funding",
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
    )
    challenge = await accounts_router.exchange_wallet_challenge(
        request,
        accounts_router.ServiceWalletChallengeForm(
            address=wallet.address,
            domain="aipg.art",
            uri="https://aipg.art/create",
            chain_id=8453,
            app_subject="user-1",
        ),
        apikey=key,
        authorization=None,
    )
    signature = Account.sign_message(
        encode_defunct(text=challenge["message"]),
        wallet.key,
    ).signature.hex()
    form = accounts_router.ServiceWalletExchangeForm(
        message=challenge["message"],
        signature=signature,
        address=wallet.address,
        app_subject="user-1",
    )

    result = await accounts_router.exchange_wallet_identity(
        request,
        form,
        apikey=key,
        authorization=None,
    )

    assert result["account_id"] == local_account["id"]
    assert str(await identities.resolve_identity("wallet", wallet.address)) == local_account["id"]
    assert str(await identities.canonical_account_id(funded_account["id"])) == local_account["id"]
    assert await credits.get_balance(UUID(local_account["id"])) == 2_000_000
    delegated = await accounts.authenticate(
        key,
        user_token=result["access_token"],
        required_scope="inference.submit",
    )
    assert delegated["service_id"] == service["id"]
    assert delegated["auth_method"] == "siwe"
    assert "account.manage" in delegated["scopes"]

    bound = await accounts_router.bind_service_identity(
        request,
        accounts_router.BindServiceIdentityForm(
            subject="local-user-1",
            user_token=result["access_token"],
        ),
        apikey=key,
        authorization=None,
    )
    assert bound["account_id"] == local_account["id"]
    assert str(
        await identities.resolve_identity(
            "app",
            f"{service['id']}:local-user-1",
        ),
    ) == local_account["id"]

    with pytest.raises(HTTPException) as replay:
        await accounts_router.exchange_wallet_identity(
            request,
            form,
            apikey=key,
            authorization=None,
        )
    assert replay.value.status_code == 401
    assert await credits.get_balance(UUID(local_account["id"])) == 2_000_000


@pytest.mark.asyncio
async def test_service_siwe_challenge_is_bound_to_service_subject_and_domain(
    db,
    monkeypatch,
):
    from eth_account import Account

    redis = _NonceRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: redis)
    _, gallery_key = await accounts.create_service_client(
        "gallery-test",
        "Gallery",
        allowed_providers=["wallet"],
        siwe_domains=["aipg.art"],
    )
    _, chat_key = await accounts.create_service_client(
        "chat-test",
        "Chat",
        allowed_providers=["wallet"],
        siwe_domains=["aipg.chat"],
    )
    wallet = Account.create()
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
    )
    challenge = await accounts_router.exchange_wallet_challenge(
        request,
        accounts_router.ServiceWalletChallengeForm(
            address=wallet.address,
            domain="aipg.art",
            uri="https://aipg.art/create",
            app_subject="gallery-user",
        ),
        apikey=gallery_key,
        authorization=None,
    )
    assert "through gallery-test" in challenge["message"]

    with pytest.raises(HTTPException) as wrong_service:
        await accounts_router.exchange_wallet_identity(
            request,
            accounts_router.ServiceWalletExchangeForm(
                message=challenge["message"],
                signature="0x00",
                address=wallet.address,
                app_subject="gallery-user",
            ),
            apikey=chat_key,
            authorization=None,
        )
    assert wrong_service.value.status_code == 401

    with pytest.raises(HTTPException) as wrong_subject:
        await accounts_router.exchange_wallet_identity(
            request,
            accounts_router.ServiceWalletExchangeForm(
                message=challenge["message"],
                signature="0x00",
                address=wallet.address,
                app_subject="other-user",
            ),
            apikey=gallery_key,
            authorization=None,
        )
    assert wrong_subject.value.status_code == 401

    with pytest.raises(HTTPException) as wrong_domain:
        await accounts_router.exchange_wallet_challenge(
            request,
            accounts_router.ServiceWalletChallengeForm(
                address=wallet.address,
                domain="aipg.chat",
                uri="https://aipg.chat",
                app_subject="gallery-user",
            ),
            apikey=gallery_key,
            authorization=None,
        )
    assert wrong_domain.value.status_code == 403


@pytest.mark.asyncio
async def test_service_identity_policy_update_requires_preview_digest(db):
    await accounts.create_service_client(
        "gallery-policy",
        "Gallery",
        allowed_providers=["app", "google"],
        google_audiences=["google-old"],
    )
    preview = await service_auth.configure_identity_policy(
        "gallery-policy",
        allowed_providers=["app", "google", "wallet"],
        google_audiences=["google-new"],
        siwe_domains=["aipg.art"],
    )
    assert preview["changed"] is True
    assert preview["applied"] is False

    with pytest.raises(ValueError, match="preview again"):
        await service_auth.configure_identity_policy(
            "gallery-policy",
            allowed_providers=["app", "google", "wallet"],
            google_audiences=["google-new"],
            siwe_domains=["aipg.art"],
            expected_digest="0" * 64,
            apply=True,
        )

    applied = await service_auth.configure_identity_policy(
        "gallery-policy",
        allowed_providers=["app", "google", "wallet"],
        google_audiences=["google-new"],
        siwe_domains=["aipg.art"],
        expected_digest=preview["current_digest"],
        apply=True,
    )
    assert applied["applied"] is True
    client = await service_auth.get_client("gallery-policy")
    assert client["allowed_providers"] == ["app", "google", "wallet"]
    assert client["google_audiences"] == ["google-new"]
    assert client["siwe_domains"] == ["aipg.art"]


@pytest.mark.asyncio
async def test_account_creation_emits_redacted_signup_alert(db, monkeypatch):
    events = []
    monkeypatch.setattr(alerts, "emit", lambda *args, **kwargs: events.append((args, kwargs)) or True)

    account, _ = await accounts.create_account(
        username="Private Person",
        email="private@example.com",
        oauth_sub="google-private-subject",
        email_verified=True,
        issue_initial_key=False,
        grant_verified_welcome=False,
    )

    assert len(events) == 1
    args, kwargs = events[0]
    assert args[:3] == ("account_created", "success", "A new Grid account was created.")
    rendered = str(kwargs)
    assert "private@example.com" not in rendered
    assert "google-private-subject" not in rendered
    assert "Private Person" not in rendered
    assert kwargs["fields"]["provider"] == "google"
    assert kwargs["fields"]["account"] == alerts.opaque_id(account["id"])


def test_user_token_signature_audience_expiry_and_step_up(monkeypatch):
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    token = user_tokens.issue(
        "00000000-0000-0000-0000-000000000001",
        audience="gallery-test",
        service_id="gallery-test",
        scopes=["account.read", "inference.submit"],
        auth_method="app",
        now=100,
    )
    assert user_tokens.verify(token, audience="gallery-test", now=101)["sub"].endswith("0001")
    with pytest.raises(HTTPException):
        user_tokens.verify(token, audience="chat-test", now=101)
    with pytest.raises(HTTPException):
        user_tokens.verify(token, now=1001)
    with pytest.raises(HTTPException):
        user_tokens.require_recent_step_up(
            {"amr": "app", "auth_time": int(time.time())},
        )


@pytest.mark.asyncio
async def test_service_key_remains_valid_for_service_owned_work(db):
    service, key = await accounts.create_service_client("worker-api", "Worker API")
    user = await accounts.authenticate(key, required_scope="inference.submit")
    assert user["key_kind"] == "service"
    assert user["service_id"] == service["id"]
    assert str(user["account_id"]) == str(service["account_id"])

    replacement = await accounts.rotate_service_key(service["id"])
    with pytest.raises(HTTPException):
        await accounts.authenticate(key)
    rotated = await accounts.authenticate(replacement, required_scope="inference.submit")
    assert rotated["service_id"] == service["id"]


@pytest.mark.asyncio
async def test_existing_key_can_be_atomically_adopted_as_bounded_service(db):
    account, key = await accounts.create_account(
        username="aigarth-bot",
        key_label="aigarth",
        is_session=False,
    )
    assert key
    service = await accounts.adopt_service_client(
        "aigarth",
        "Aigarth",
        account_id=account["id"],
        key_label="aigarth",
        allowed_providers=["app"],
        per_request_micro=500_000,
        daily_micro=100_000_000,
    )
    assert service["account_id"] == account["id"]

    user = await accounts.authenticate(key, required_scope="inference.submit")
    assert user["key_kind"] == "service"
    assert user["service_id"] == "aigarth"
    assert user["service_limits"] == {
        "per_request_micro": 500_000,
        "daily_micro": 100_000_000,
    }
    assert quota.is_paid(user) is True

    # Exact re-runs are idempotent and keep the same plaintext key valid.
    again = await accounts.adopt_service_client(
        "aigarth",
        "Aigarth",
        account_id=account["id"],
        key_label="aigarth",
        allowed_providers=["app"],
        per_request_micro=500_000,
        daily_micro=100_000_000,
    )
    assert again == service
    assert (await accounts.authenticate(key))["service_id"] == "aigarth"


@pytest.mark.asyncio
async def test_service_adoption_rolls_back_when_key_label_is_ambiguous(db):
    account, original = await accounts.create_account(
        username="ambiguous",
        key_label="shared",
        is_session=False,
    )
    assert original
    await accounts.issue_key(UUID(account["id"]), label="shared")
    with pytest.raises(ValueError, match="exactly one"):
        await accounts.adopt_service_client(
            "ambiguous-service",
            "Ambiguous",
            account_id=account["id"],
            key_label="shared",
            per_request_micro=100,
            daily_micro=1_000,
        )

    async with await database.new_session() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(service_clients_table)
            .where(service_clients_table.c.id == "ambiguous-service"),
        )
    assert count == 0
    assert (await accounts.authenticate(original))["key_kind"] == "user"


@pytest.mark.asyncio
async def test_service_creation_is_atomic_on_duplicate_id(db):
    await accounts.create_service_client("atomic-test", "First")
    async with await database.new_session() as session:
        before = await session.scalar(sa.select(sa.func.count()).select_from(accounts_table))
    with pytest.raises(IntegrityError):
        await accounts.create_service_client("atomic-test", "Duplicate")
    async with await database.new_session() as session:
        after = await session.scalar(sa.select(sa.func.count()).select_from(accounts_table))
    assert after == before


class _LimitRedis:
    def __init__(self):
        self.used = 0
        self.refs: dict[str, str] = {}

    async def get(self, key):
        return self.refs.get(key)

    async def eval(self, script, _key_count, _spend_key, ref_key, *args):
        if script == service_limits._RELEASE_LUA:
            expected, amount = args
            if self.refs.get(ref_key) != expected:
                return 0
            self.used = max(self.used - int(amount), 0)
            del self.refs[ref_key]
            return 1
        if script == service_limits._RECONCILE_LUA:
            expected, reserved, keep, day = args
            if self.refs.get(ref_key) != expected:
                return 0
            self.used = max(self.used - (int(reserved) - int(keep)), 0)
            self.refs[ref_key] = f"{day}:{keep}"
            return 1
        amount, cap, _ttl, day = args
        if ref_key in self.refs:
            return 1
        if int(cap) > 0 and self.used + int(amount) > int(cap):
            return 0
        self.used += int(amount)
        self.refs[ref_key] = f"{day}:{amount}"
        return 1


@pytest.mark.asyncio
async def test_service_spending_limits_are_idempotent_and_fail_closed(db, monkeypatch):
    redis = _LimitRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: redis)

    async def ignore_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service_limits, "record_event", ignore_event)
    user = {
        "service_id": "limits-test",
        "account_id": "00000000-0000-0000-0000-000000000001",
        "service_limits": {"per_request_micro": 600, "daily_micro": 1_000},
    }
    assert await service_limits.authorize(user, 500, "job-1") == (True, None)
    assert await service_limits.authorize(user, 500, "job-1") == (True, None)
    assert redis.used == 500
    assert await service_limits.reconcile("limits-test", "job-1", 200) is True
    assert await service_limits.reconcile("limits-test", "job-1", 200) is True
    assert redis.used == 200
    assert await service_limits.authorize(user, 300, "job-release") == (True, None)
    assert redis.used == 500
    assert await service_limits.release("limits-test", "job-release") is True
    assert await service_limits.release("limits-test", "job-release") is False
    assert redis.used == 200
    allowed, reason = await service_limits.authorize(user, 501, "job-2")
    assert allowed and reason is None
    assert redis.used == 701
    allowed, reason = await service_limits.authorize(user, 300, "job-over")
    assert not allowed and "daily" in reason
    allowed, reason = await service_limits.authorize(user, 601, "job-3")
    assert not allowed and "per-request" in reason

    def broken_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr("grid_api.redis_client.get_redis", broken_redis)
    allowed, reason = await service_limits.authorize(user, 100, "job-4")
    assert not allowed and "unavailable" in reason
