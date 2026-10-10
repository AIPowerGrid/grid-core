# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from grid_api.models.openai import ChatCompletionRequest
from grid_api.routers import openai, videos
from grid_api.services import media, recipes, storage
from grid_api.services.recipe_import import recipe_root

MODEL = "LTX-2.5 Distilled BF16"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(videos.limiter, "enabled", False)
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    root = Path(__file__).resolve().parents[3]
    for path in (root / "recipes/qualification").glob("*.json"):
        wf = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(wf), wf["_grid"]["name"], wf)
    monkeypatch.setattr(videos.accounts_svc, "authenticate", AsyncMock(return_value={"account_id": "test"}))
    monkeypatch.setattr(videos, "get_available_models", AsyncMock(return_value=[MODEL, "LTX-2.3"]))
    quota = AsyncMock()
    monkeypatch.setattr(videos.quota, "check_and_consume", quota)
    submit = AsyncMock(return_value=([{"url": "https://media.invalid/video.mp4", "seed": 0}], {"job_id": "test"}))
    monkeypatch.setattr(media, "submit_and_wait", submit)
    monkeypatch.setattr(videos.loras_svc, "prepare_loras", lambda *_args: [])
    app = FastAPI()
    app.include_router(videos.router)
    return app, submit, quota


async def post(app, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/videos/generations", json={"prompt": "a red ball", "model": MODEL, **payload}, headers={"apikey": "test-key"})


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,expected", [({}, 81), ({"seconds": 4}, 97), ({"frames": 297}, 297)])
async def test_http_contract_preserves_execution_defaults(setup, overrides, expected):
    app, submit, _quota = setup
    response = await post(app, {"seed": 0, **overrides})
    assert response.status_code == 200, response.text
    payload = submit.call_args.args[2]
    assert (payload["width"], payload["height"]) == (960, 544)
    assert payload["steps"] == 8
    assert payload["cfg_scale"] == 1
    assert payload["frames"] == expected
    assert payload["seed"] == 0
    assert payload["recipe_inputs"]["seconds"] == expected / 24
    assert payload["recipe_inputs"]["sampler"] == "euler_ancestral_cfg_pp"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"frames": 96}, {"frames": 81, "seconds": 4}, {"size": "768x768"},
    {"steps": 30}, {"cfg_scale": 2}, {"fps": 30}, {"n": 2}, {"audio": False},
    {"timeline": "{}"}, {"scheduler": "normal"}, {"seed": True},
    {"loras": [{"name": "not-qualified"}]}, {"response_format": "unknown"},
    {"keyframes": [{"image": "secret-private-image", "frame": 80}]},
    {"image": "source", "keyframes": [{"image": "other", "frame": 0}]},
    {"keyframes": [{"image": "first", "frame": 0}, {"image": "last", "frame": 80}]},
    {"keyframes": [{"image": "first", "frame": 0, "strength": 0.5}]},
    {"model": "LTX-2.3", "frames": 81}, {"model": "LTX-2.3", "seconds": 12},
])
async def test_http_rejects_before_quota_upload_or_dispatch(setup, monkeypatch, payload):
    app, submit, quota = setup
    upload = AsyncMock()
    monkeypatch.setattr(media, "prepare_source_image", upload)
    response = await post(app, payload)
    assert response.status_code == 422, response.text
    assert "secret-private-image" not in response.text
    quota.assert_not_awaited()
    upload.assert_not_awaited()
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_first_frame_uses_existing_private_upload_path(setup, monkeypatch):
    app, submit, _quota = setup
    stream = io.BytesIO()
    Image.new("RGB", (1920, 1080), "white").save(stream, "PNG")
    image = base64.b64encode(stream.getvalue()).decode()
    uploads = []

    def upload(raw, ext):
        uploads.append((raw, ext))
        return "https://upload.invalid/grid-source.png"

    monkeypatch.setattr(storage, "upload_source", upload)
    response = await post(app, {"keyframes": [{"image": image, "frame": 0}]})
    assert response.status_code == 200, response.text
    payload = submit.call_args.args[2]
    assert payload["source_image_url"] == "https://upload.invalid/grid-source.png"
    assert payload["width"] % 32 == payload["height"] % 32 == 0
    assert payload["width"] * payload["height"] <= 960 * 544
    assert uploads == [(stream.getvalue(), "png")]
    assert "keyframes" not in payload  # No client-controlled graph or paths.


@pytest.mark.asyncio
@pytest.mark.parametrize("model,steps,cfg", [(MODEL, 8, 1), ("LTX-2.5 Dev BF16", 40, 2)])
async def test_chat_video_uses_the_same_units(setup, model, steps, cfg):
    _app, submit, _quota = setup
    request = ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "a red ball"}], seed=0)
    await openai._chat_media(request, "video", "test", user={"account_id": "test"})
    payload = submit.call_args.args[2]
    assert payload["frames"] == 81
    assert payload["steps"] == steps
    assert payload["cfg_scale"] == cfg
    assert payload["seed"] == 0
    assert payload["recipe_inputs"]["seconds"] == 81 / 24
