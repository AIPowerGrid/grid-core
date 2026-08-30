# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from grid_api import database
from grid_api.routers import accounts as accounts_router
from grid_api.services import accounts, credits
from grid_api.v2.schema import metadata


async def _zero(*_args, **_kwargs):
    return 0


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    previous = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield
    finally:
        database._session_factory = previous
        await engine.dispose()


@pytest.mark.asyncio
async def test_quote_returns_canonical_balance_and_exact_image_cost(db, monkeypatch):
    monkeypatch.setattr("grid_api.services.promotions.available_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    account, key = await accounts.create_account(
        username="Quote test",
        issue_initial_key=True,
    )
    account_id = UUID(account["id"])
    assert await credits.credit(account_id, 20_000, "test_funding", "quote:test-funding")

    result = await accounts_router.quote_credits(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        accounts_router.CreditQuoteForm(
            model="Krea 2 Turbo",
            modality="image",
            n=2,
        ),
        apikey=key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=None,
    )

    assert result["account_id"] == str(account_id)
    assert result["paid"] == {"balance_micro": 20_000, "balance_usd": 0.02}
    assert result["total_spendable_micro"] == 20_000
    assert result["estimate"]["priced"] is True
    assert result["estimate"]["cost_micro"] == 10_000
    assert result["estimate"]["cost_usd"] == 0.01
    assert result["estimate"]["from_paid_micro"] == 10_000
    assert result["estimate"]["shortfall_micro"] == 0
    assert result["estimate"]["balance_sufficient"] is True


@pytest.mark.asyncio
async def test_quote_reports_shortfall_without_moving_value(db, monkeypatch):
    monkeypatch.setattr("grid_api.services.promotions.available_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    account, key = await accounts.create_account(
        username="Short quote",
        issue_initial_key=True,
    )
    account_id = UUID(account["id"])
    assert await credits.credit(account_id, 4_000, "test_funding", "quote:short-funding")

    result = await accounts_router.quote_credits(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        accounts_router.CreditQuoteForm(
            model="Krea 2 Turbo",
            modality="image",
        ),
        apikey=key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=None,
    )

    assert result["estimate"]["cost_micro"] == 5_000
    assert result["estimate"]["from_paid_micro"] == 4_000
    assert result["estimate"]["shortfall_micro"] == 1_000
    assert result["estimate"]["balance_sufficient"] is False
    assert await credits.get_balance(account_id) == 4_000


@pytest.mark.asyncio
async def test_quote_uses_only_active_pockets_in_spending_order(db, monkeypatch):
    async def promo(*_args, **_kwargs):
        return 2_000

    async def daily(*_args, **_kwargs):
        return 1_000

    monkeypatch.setattr("grid_api.services.promotions.available_micro", promo)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_ENABLED", True)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_SPENDABLE_LIVE", True)
    monkeypatch.setattr(
        "grid_api.services.promotions.PROMO_SPENDABLE_CAMPAIGNS",
        frozenset({"builder-test"}),
    )
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", daily)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", daily)
    monkeypatch.setattr("grid_api.services.free_credits.FREE_ENABLED", True)
    monkeypatch.setattr("grid_api.services.free_credits.FREE_SPENDABLE_LIVE", True)
    account, key = await accounts.create_account(
        username="Pocket quote",
        issue_initial_key=True,
    )
    account_id = UUID(account["id"])
    assert await credits.credit(account_id, 4_000, "test_funding", "quote:pocket-funding")

    result = await accounts_router.quote_credits(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        accounts_router.CreditQuoteForm(
            model="Krea 2 Turbo",
            modality="image",
        ),
        apikey=key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=None,
    )

    assert result["total_spendable_micro"] == 7_000
    assert result["estimate"]["from_promotional_micro"] == 2_000
    assert result["estimate"]["from_daily_micro"] == 1_000
    assert result["estimate"]["from_paid_micro"] == 2_000
    assert result["estimate"]["shortfall_micro"] == 0


@pytest.mark.asyncio
async def test_quote_excludes_unallowlisted_promo_from_spendable_balance(db, monkeypatch):
    async def promo(*_args, spendable_only=False, **_kwargs):
        return 0 if spendable_only else 2_000

    monkeypatch.setattr("grid_api.services.promotions.available_micro", promo)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_ENABLED", True)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_SPENDABLE_LIVE", True)
    monkeypatch.setattr(
        "grid_api.services.promotions.PROMO_SPENDABLE_CAMPAIGNS",
        frozenset({"builder-test"}),
    )
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    account, key = await accounts.create_account(
        username="Unallowlisted promo quote",
        issue_initial_key=True,
    )
    account_id = UUID(account["id"])
    assert await credits.credit(account_id, 4_000, "test_funding", "quote:promo-filter")

    result = await accounts_router.quote_credits(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        accounts_router.CreditQuoteForm(
            model="Krea 2 Turbo",
            modality="image",
        ),
        apikey=key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=None,
    )

    assert result["promotional"] == {
        "remaining_micro": 0,
        "remaining_usd": 0.0,
        "preview_remaining_micro": 2_000,
        "preview_remaining_usd": 0.002,
        "active": True,
    }
    assert result["total_spendable_micro"] == 4_000
    assert result["total_preview_micro"] == 6_000
    assert result["estimate"]["from_promotional_micro"] == 0
    assert result["estimate"]["shortfall_micro"] == 1_000


@pytest.mark.asyncio
async def test_dark_promo_keeps_legacy_preview_display_without_becoming_spendable(db, monkeypatch):
    async def promo(*_args, spendable_only=False, **_kwargs):
        return 0 if spendable_only else 2_000

    monkeypatch.setattr("grid_api.services.promotions.available_micro", promo)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_ENABLED", True)
    monkeypatch.setattr("grid_api.services.promotions.PROMO_SPENDABLE_LIVE", True)
    monkeypatch.setattr(
        "grid_api.services.promotions.PROMO_SPENDABLE_CAMPAIGNS",
        frozenset(),
    )
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    account, _key = await accounts.create_account(
        username="Dark promo display",
        issue_initial_key=True,
    )
    account_id = UUID(account["id"])
    assert await credits.credit(account_id, 4_000, "test_funding", "summary:promo-dark")

    result = await credits.account_credit_summary({"account_id": account_id})

    assert result["promotional"]["active"] is False
    assert result["promotional"]["remaining_micro"] == 2_000
    assert result["promotional"]["preview_remaining_micro"] == 2_000
    assert result["total_spendable_micro"] == 4_000
    assert result["total_preview_micro"] == 6_000


@pytest.mark.asyncio
async def test_unpriced_quote_is_not_reported_as_free(db, monkeypatch):
    monkeypatch.setattr("grid_api.services.promotions.available_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    _account, key = await accounts.create_account(
        username="Unpriced quote",
        issue_initial_key=True,
    )

    result = await accounts_router.quote_credits(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        accounts_router.CreditQuoteForm(
            model="unknown-model",
            modality="image",
        ),
        apikey=key,
        authorization=None,
        x_grid_user_assertion=None,
        x_grid_user_token=None,
    )

    assert result["estimate"]["priced"] is False
    assert result["estimate"]["reason"] == "unpriced"
    assert result["estimate"]["cost_micro"] is None
    assert result["estimate"]["balance_sufficient"] is False


def test_quote_request_is_strict_and_bounded():
    with pytest.raises(ValidationError):
        accounts_router.CreditQuoteForm(model="ltx-2.3", modality="video")
    with pytest.raises(ValidationError):
        accounts_router.CreditQuoteForm(
            model="ace-step-v1.5-xl-turbo",
            modality="audio",
            seconds=3_601,
        )
    with pytest.raises(ValidationError):
        accounts_router.CreditQuoteForm(
            model="z-image-turbo",
            modality="image",
            n=17,
        )
    with pytest.raises(ValidationError):
        accounts_router.CreditQuoteForm(
            model="z-image-turbo",
            modality="image",
            unknown=True,
        )
