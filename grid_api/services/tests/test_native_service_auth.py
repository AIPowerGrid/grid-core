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
from grid_api.services import accounts, quota, service_limits, user_tokens
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
        self.refs: set[str] = set()

    async def eval(self, _script, _key_count, _spend_key, ref_key, amount, cap, _ttl):
        if ref_key in self.refs:
            return 1
        if int(cap) > 0 and self.used + int(amount) > int(cap):
            return 0
        self.used += int(amount)
        self.refs.add(ref_key)
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
    allowed, reason = await service_limits.authorize(user, 501, "job-2")
    assert not allowed and "daily" in reason
    allowed, reason = await service_limits.authorize(user, 601, "job-3")
    assert not allowed and "per-request" in reason

    def broken_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr("grid_api.redis_client.get_redis", broken_redis)
    allowed, reason = await service_limits.authorize(user, 100, "job-4")
    assert not allowed and "unavailable" in reason
