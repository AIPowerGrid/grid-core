# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path
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


@pytest.mark.asyncio
async def test_unknown_text_quote_matches_nonzero_reservation(db, monkeypatch):
    monkeypatch.setattr("grid_api.services.promotions.available_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    account, _key = await accounts.create_account(username="Default tariff", issue_initial_key=True)
    aid = UUID(account["id"])
    await credits.credit(aid, 10000, "test_funding", "default-quote-funding")
    user = {"account_id": aid}
    quote = await credits.quote_for_account(user, model="new-text-model", modality="text", prompt_tokens=1000, max_tokens=1000)
    assert quote["estimate"]["priced"]
    assert quote["estimate"]["cost_micro"] == 375
    assert await credits.get_balance(aid) == 10000
    auth = await credits.authorize_request(user, "new-text-model", 1000, 1000, "default-quote-job", record_reservation=True)
    assert auth["reserved"] == quote["estimate"]["cost_micro"]


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


def test_quote_accepts_extended_bf16_frame_ceiling():
    form = accounts_router.CreditQuoteForm(model="ltx-2.5-distilled-bf16", modality="video", frames=625)
    assert form.frames == 625
    with pytest.raises(ValidationError):
        accounts_router.CreditQuoteForm(model=form.model, modality="video", frames=722)


@pytest.mark.asyncio
@pytest.mark.parametrize("priced", [False, True])
async def test_ingredients_quote_needs_only_timing_and_moves_no_value(db, monkeypatch, priced):
    from grid_api.services import recipes, pricing, video_contract
    from grid_api.services.recipe_import import recipe_root

    for name in ("_BY_ROOT", "_BY_ID", "_BY_NAME", "_BY_MODEL"):
        monkeypatch.setattr(recipes, name, {})
    path = Path(__file__).parents[3] / "recipes/qualification/ltx-2.5-dev-bf16-ingredients-v1.json"
    graph = json.loads(path.read_text())
    model = graph["_grid"]["modelName"]
    recipes.register_recipe(recipe_root(graph), graph["_grid"]["name"], graph)
    if priced:
        monkeypatch.setitem(pricing.PRICING, model.lower(), pricing.ModelPrice(0, 0, video_per_second=0.02))
    else:
        monkeypatch.delitem(pricing.PRICING, model.lower(), raising=False)
    monkeypatch.setattr("grid_api.services.promotions.available_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.daily_cap_micro", _zero)
    monkeypatch.setattr("grid_api.services.free_credits.available_micro", _zero)
    account, key = await accounts.create_account(username="Appearance quote", issue_initial_key=True)
    aid = UUID(account["id"])
    await credits.credit(aid, 1_000_000, "test_funding", "ingredients-quote-funding")
    summary = await credits.account_credit_summary({"account_id": aid})
    for fields in ({"frames": 121}, {"seconds": 121 / 24}):
        quote = await accounts_router.quote_credits(
            Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
            accounts_router.CreditQuoteForm(model=model, modality="video", **fields),
            apikey=key, authorization=None, x_grid_user_assertion=None, x_grid_user_token=None)
        estimate = quote["estimate"]
        assert (estimate["frames"], estimate["fps"], estimate["seconds"]) == (121, 24, 121 / 24)
        assert estimate["priced"] is priced
        assert estimate["cost_micro"] == (100_834 if priced else None)
        assert await credits.account_credit_summary({"account_id": aid}) == summary
    async with await database.new_session() as session:
        import sqlalchemy as sa
        from grid_api.v2 import schema
        assert await session.scalar(sa.select(sa.func.count()).select_from(schema.reservations)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(schema.media_jobs)) == 0
    with pytest.raises(ValueError, match="separate appearance image"):
        video_contract.normalize(recipes.video_contract_for_model(model), {"frames": 121})


@pytest.mark.parametrize("variant,maximum,appearance,frames,seconds", [
    ("dev", 81, False, 121, None),
    ("dev", 121, True, 81, None),
    ("dev", 121, True, 122, None),
    ("dev", 121, True, 121, 4.0),
    ("distilled", 337, False, 338, None),
])
def test_shared_quote_timing_does_not_widen_execution(variant, maximum, appearance, frames, seconds):
    from grid_api.services.video_contract import LtxVideoContract, normalize_timing
    contract = LtxVideoContract.model_validate({
        "schema": "aipg.ltx-video.v1", "variant": variant, "max_frames": maximum,
        "max_pixels": 768 * 448,
        **({"appearance_profile": "ingredients-dev-v1", "source_modes": ["audio", "video"]}
           if appearance else {})})
    with pytest.raises(ValueError):
        normalize_timing(contract, frames=frames, seconds=seconds)
