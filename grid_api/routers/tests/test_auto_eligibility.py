# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Auto eligibility is checked before quota, paid reservation, or dispatch."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from grid_api.models.openai import ChatCompletionRequest
from grid_api.routers import openai as o, stats
from grid_api.services import credits, router as r


class ReachedQuota(Exception):
    pass


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("GRID_ROUTING_PIN", raising=False)
    monkeypatch.delenv("GRID_ROUTING_CONFIG", raising=False)
    monkeypatch.setattr(o, "_detect_media_model", AsyncMock(return_value=None))
    monkeypatch.setattr(stats, "_active_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr(r, "get_model_scores", AsyncMock(return_value={
        "gpt-oss-20b": {"score": 100}, "qwen3-27b": {"score": -1},
    }))
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    gate = AsyncMock(side_effect=ReachedQuota)
    monkeypatch.setattr(o.quota, "check_and_consume", gate)
    return gate


@pytest.mark.asyncio
async def test_unpriced_pin_cannot_override_billing_eligibility(monkeypatch, setup):
    # Reproduces the incident with a connected, fastest but unpriced model.
    monkeypatch.setenv("GRID_ROUTING_PIN", "gpt-oss-20b")
    monkeypatch.setattr(o, "get_available_models", AsyncMock(return_value=["gpt-oss-20b", "qwen3-27b"]))
    request = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(ReachedQuota):
        await o._handle_chat_completions_for_user(request, {})
    assert request.model == "qwen3-27b"


@pytest.mark.asyncio
async def test_all_ineligible_returns_503_before_quota(monkeypatch, setup):
    monkeypatch.setenv("GRID_ROUTING_PIN", "gpt-oss-20b")
    monkeypatch.setattr(o, "get_available_models", AsyncMock(return_value=["gpt-oss-20b"]))
    request = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(HTTPException) as exc:
        await o._handle_chat_completions_for_user(request, {})
    assert exc.value.status_code == 503
    setup.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_model_not_silently_replaced(monkeypatch, setup):
    monkeypatch.setattr(o, "get_available_models", AsyncMock(return_value=["gpt-oss-20b", "qwen3-27b"]))
    request = ChatCompletionRequest(model="gpt-oss-20b", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(ReachedQuota):
        await o._handle_chat_completions_for_user(request, {})
    assert request.model == "gpt-oss-20b"


@pytest.mark.asyncio
async def test_free_request_does_not_invent_a_charging_requirement(monkeypatch, setup):
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    monkeypatch.setenv("GRID_ROUTING_PIN", "gpt-oss-20b")
    monkeypatch.setattr(o, "get_available_models", AsyncMock(return_value=["gpt-oss-20b"]))
    request = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(ReachedQuota):
        await o._handle_chat_completions_for_user(request, {})
    assert request.model == "gpt-oss-20b"


@pytest.mark.asyncio
async def test_x402_requires_price_even_when_global_charging_off(monkeypatch, setup):
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    monkeypatch.setenv("GRID_ROUTING_PIN", "gpt-oss-20b")
    monkeypatch.setattr(o, "get_available_models", AsyncMock(return_value=["gpt-oss-20b"]))
    request = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(HTTPException) as exc:
        await o._handle_chat_completions_for_user(request, {}, x402_payment=({}, {}))
    assert exc.value.status_code == 503
    setup.assert_not_awaited()
