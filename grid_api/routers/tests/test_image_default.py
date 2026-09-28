# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from grid_api.routers import images
from grid_api.services import media, pricing


def test_default_image_model_matches_governed_recipe_and_price():
    root = Path(__file__).resolve().parents[3]
    recipe = json.loads((root / "recipes/flux2-klein-t2i.json").read_text())
    assert media.DEFAULT_IMAGE_MODEL == recipe["_grid"]["modelName"]
    assert pricing.quote_image(media.DEFAULT_IMAGE_MODEL, 1) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,online,status", [
    (None, ["FLUX.2 Klein 4B FP8"], 200),
    ("z-image-turbo", ["z-image-turbo"], 200),
    (None, ["z-image-turbo"], 404),
    (None, [], 503),
])
async def test_default_routes_without_overriding_explicit_model(monkeypatch, selected, online, status):
    monkeypatch.setattr(images.limiter, "enabled", False)
    user = {"account_id": "test-account"}
    monkeypatch.setattr(images.accounts_svc, "authenticate", AsyncMock(return_value=user))
    monkeypatch.setattr(images, "get_available_models", AsyncMock(return_value=online))
    quota = AsyncMock()
    monkeypatch.setattr(images.quota, "check_and_consume", quota)
    monkeypatch.setattr(images.loras_svc, "prepare_loras", lambda *_args: [])
    submit = AsyncMock(return_value=([{"url": "https://media.invalid/test.webp"}], {"job_id": "test-job"}))
    monkeypatch.setattr(images.media, "submit_and_wait", submit)
    app = FastAPI()
    app.include_router(images.router)
    payload = {"prompt": "a lighthouse", **({"model": selected} if selected else {})}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/images/generations", json=payload, headers={"apikey": "test-key"})
    assert response.status_code == status
    if status == 200:
        assert submit.call_args.args[0] == (selected or "FLUX.2 Klein 4B FP8")
        assert submit.call_args.kwargs["account_id"] == "test-account"
        assert submit.call_args.kwargs["billing_user"] == user
    else:
        quota.assert_not_awaited()
        submit.assert_not_awaited()
