# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import io
import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from PIL import Image

from grid_api.models.openai import ChatCompletionRequest
from grid_api.routers import openai, videos
from grid_api.services import media, recipes, storage, video_sources
from grid_api.services.recipe_import import recipe_root

MODEL = "LTX-2.5 Distilled BF16"


@pytest.mark.asyncio
async def test_startup_inventory_keeps_legacy_recipe_shape(setup):
    root = Path(__file__).resolve().parents[3]
    spec = json.loads((root / "recipes/flux2-klein-t2i.json").read_text())
    recipes.register_recipe(recipe_root(spec), spec["_grid"]["name"], spec)
    model = spec["_grid"]["modelName"]
    app = FastAPI()
    app.include_router(openai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/models/{model}/recipe", headers={"apikey": "test-key"})
    assert response.status_code == 200
    for record in response.json()["recipes"]:
        assert set(record) == {
            "name", "recipe_root", "job_type", "required_models", "node_types",
            "model_files", "image_paths", "spec",
        }


@pytest.mark.asyncio
async def test_startup_inventory_still_requires_authentication(setup, monkeypatch):
    from grid_api.services import accounts
    auth = AsyncMock(side_effect=HTTPException(status_code=401, detail="Invalid API key"))
    monkeypatch.setattr(accounts, "authenticate", auth)
    app = FastAPI()
    app.include_router(openai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/models/{MODEL}/recipe")
        assert response.status_code == 401
        auth.assert_not_awaited()
        response = await client.get(f"/v1/models/{MODEL}/recipe", headers={"apikey": "invalid-key"})
    assert response.status_code == 401
    assert "recipes" not in response.json()
    auth.assert_awaited_once_with("invalid-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("model,variant,modes", [
    (MODEL, "distilled", {"audio", "video"}),
    ("LTX-2.5 Dev BF16", "dev", set()),
])
async def test_startup_recipe_inventory_includes_governed_source_requirements(
    setup, model, variant, modes,
):
    originals = {r.recipe_root: json.dumps(r.spec, sort_keys=True)
                 for r in recipes.recipes_for_model(model)}
    app = FastAPI()
    app.include_router(openai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        response = await client.get(f"/v1/models/{model}/recipe", headers={"apikey": "test-key"})
    assert response.status_code == 200
    records = response.json()["recipes"]
    assert len(records) == 2
    for record in records:
        contract = record["video_contract"]
        assert contract["schema"] == "aipg.ltx-video.v1"
        assert contract["variant"] == variant
        assert set(contract["source_modes"]) == modes
        assert set(record["source_requirements"]) == modes
        for requirements in record["source_requirements"].values():
            assert set(record["node_types"]) - {"LTXVAudioVAEDecode", "LTXVEmptyLatentAudio"} <= set(requirements["node_types"])
            assert {"LoadAudio", "LTXVAudioVAEEncode", "SetLatentNoiseMask"} <= set(requirements["node_types"])
        if "video" in modes:
            video = record["source_requirements"]["video"]
            assert {"LoadVideo", "GetVideoComponents", "Canny", "LTXAddVideoICLoRAGuide"} <= set(video["node_types"])
            assert "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors" in video["model_files"]
        assert "source_audio" not in record["spec"]
        assert originals[record["recipe_root"]] == json.dumps(
            recipes.get_recipe(record["recipe_root"]).spec, sort_keys=True,
        )


@pytest.fixture
def setup(monkeypatch):
    from grid_api.config import GridSettings
    monkeypatch.setattr(videos.generation_admission, "get_settings", lambda: GridSettings(
        _env_file=None, media_async_enabled=True, media_async_admission_mode="open",
        typed_video_waiting_enabled=True))
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
    monkeypatch.setattr(videos.generation_admission, "require_media", lambda *_args: None)
    monkeypatch.setattr(storage, "private_source_bucket", lambda: "private-synthetic-source")
    app = FastAPI()
    app.include_router(videos.router)
    return app, submit, quota


async def post(app, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/videos/generations", json={"prompt": "a red ball", "model": MODEL, **payload}, headers={"apikey": "test-key"})


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_pilot_cannot_bypass_async_admission_through_waiting_route(setup, monkeypatch, public):
    from grid_api.config import GridSettings
    app, submit, quota = setup
    monkeypatch.setattr(videos.generation_admission, "get_settings", lambda: GridSettings(
        _env_file=None, media_async_enabled=public, media_async_admission_mode="open" if public else "pilot"))
    response = await post(app, {"image": "invalid-inline-data"})
    assert response.status_code == 503
    submit.assert_not_awaited()
    quota.assert_not_awaited()
    # Ordinary LTX-2.3 does not acquire the new governed-pilot restriction.
    response = await post(app, {"model": "LTX-2.3"})
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_pilot_chat_and_shared_waiter_reject_before_upload_or_credit(setup, monkeypatch, public):
    from grid_api.config import GridSettings
    monkeypatch.setattr(videos.generation_admission, "get_settings", lambda: GridSettings(
        _env_file=None, media_async_enabled=public, media_async_admission_mode="open" if public else "pilot"))
    source = AsyncMock()
    monkeypatch.setattr(media, "prepare_source_image", source)
    body = ChatCompletionRequest(model=MODEL, messages=[{"role": "user", "content": "synthetic"}])
    with pytest.raises(HTTPException) as denied:
        await openai._chat_media(body, "video", account_id="test")
    assert denied.value.status_code == 503
    source.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,expected", [({}, 81), ({"seconds": 4}, 97), ({"frames": 297}, 297),
                                                ({"frames": 481}, 481), ({"seconds": 20}, 481),
                                                ({"frames": 625}, 625), ({"seconds": 26}, 625)])
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
    {"keyframes": [{"image": "first", "frame": 0}, {"image": "last", "frame": 81}]},
    {"keyframes": [{"image": "first", "frame": 0, "strength": 1.5}]},
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
async def test_typed_first_frame_uses_separate_private_upload_path(setup, monkeypatch):
    app, submit, _quota = setup
    stream = io.BytesIO()
    Image.new("RGB", (1920, 1080), "white").save(stream, "PNG")
    image = base64.b64encode(stream.getvalue()).decode()
    uploads = []

    def upload(raw, ext):
        uploads.append((raw, ext))
        return "https://upload.invalid/grid-source.png"

    monkeypatch.setattr(storage, "upload_private_source", upload)
    response = await post(app, {"keyframes": [{"image": image, "frame": 0}]})
    assert response.status_code == 200, response.text
    payload = submit.call_args.args[2]
    assert payload["source_image_url"] == "https://upload.invalid/grid-source.png"
    assert payload["width"] % 32 == payload["height"] % 32 == 0
    assert payload["width"] * payload["height"] <= 960 * 544
    assert uploads == [(stream.getvalue(), "png")]
    assert "keyframes" not in payload  # No client-controlled graph or paths.


@pytest.mark.asyncio
async def test_timed_pins_are_uploaded_once_and_keep_their_positions(setup, monkeypatch):
    app, submit, _quota = setup
    stream = io.BytesIO()
    Image.new("RGB", (960, 544), "white").save(stream, "PNG")
    image = base64.b64encode(stream.getvalue()).decode()
    upload = lambda *_args: "https://upload.invalid/reference.png"
    monkeypatch.setattr(storage, "upload_private_source", upload)
    response = await post(app, {"frames": 337, "keyframes": [
        {"image": image, "frame": 0}, {"image": image, "frame": 281, "strength": 0.5}]})
    assert response.status_code == 200, response.text
    payload = submit.call_args.args[2]
    assert payload["source_image_urls"] == ["https://upload.invalid/reference.png"] * 2
    assert payload["recipe_inputs"]["keyframes"] == [{"frame": 0, "strength": 1.0}, {"frame": 281, "strength": 0.5}]
    assert payload["frames"] == 337
    assert image not in str(payload)
    videos.get_available_models.assert_awaited_once_with(job_type="video", media_feature="recipe-image-bindings-v1")


@pytest.mark.asyncio
async def test_bad_later_image_rejects_before_quota_or_any_upload(setup, monkeypatch):
    app, submit, quota = setup
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(stream, "PNG")
    image = base64.b64encode(stream.getvalue()).decode()
    upload = AsyncMock()
    monkeypatch.setattr(media, "upload_video_keyframes", upload)
    response = await post(app, {"keyframes": [{"image": image, "frame": 0}, {"image": "invalid", "frame": 80}]})
    assert response.status_code == 400, response.text
    quota.assert_not_awaited()
    upload.assert_not_awaited()
    submit.assert_not_awaited()


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


def reference_audio(frames=9):
    data = io.BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setparams((2, 2, 48_000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0" * frames * 2000 * 4)
    return base64.b64encode(data.getvalue()).decode()


@pytest.mark.asyncio
async def test_audio_ingress_uploads_committed_source_not_executable_inputs(setup, monkeypatch):
    app, submit, _quota = setup
    monkeypatch.setattr(storage, "upload_private_source", lambda *_args: "https://source.invalid/private.wav")
    encoded = reference_audio()
    response = await post(app, {"frames": 9, "reference_audio": encoded})
    assert response.status_code == 200, response.text
    body = submit.call_args.args[2]
    assert body["recipe_inputs"]["source_mode"] == "audio"
    assert body["source_media"][0]["samples"] == 18_000
    assert body["source_media"][0]["role"] == "reference_audio"
    assert body["required_media_features"] == ["recipe-media-sources-v1"]
    assert encoded not in str(body)
    assert "source_image_url" not in body


@pytest.mark.asyncio
async def test_default_dark_source_path_rejects_before_decoder_or_storage(setup, monkeypatch):
    app, submit, quota = setup
    def closed(*_args):
        raise videos.HTTPException(503, detail="closed source path")
    monkeypatch.setattr(videos.generation_admission, "require_media", closed)
    validate = AsyncMock()
    monkeypatch.setattr(video_sources, "validate_sources", validate)
    response = await post(app, {"frames": 9, "reference_audio": reference_audio()})
    assert response.status_code == 503
    validate.assert_not_awaited()
    quota.assert_not_awaited()
    submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("image", [False, True])
async def test_missing_private_storage_rejects_before_quota_or_any_upload(setup, monkeypatch, image):
    app, submit, quota = setup

    def unavailable():
        raise videos.HTTPException(503, detail="Private video source storage is unavailable")

    monkeypatch.setattr(storage, "private_source_bucket", unavailable)
    upload = AsyncMock()
    monkeypatch.setattr(video_sources, "upload_sources", upload)
    monkeypatch.setattr(media, "upload_video_keyframes", upload)
    if image:
        stream = io.BytesIO()
        Image.new("RGB", (256, 256), "white").save(stream, "PNG")
        values = {"keyframes": [{"image": base64.b64encode(stream.getvalue()).decode(), "frame": 0}]}
    else:
        values = {"frames": 9, "reference_audio": reference_audio()}
    response = await post(app, values)
    assert response.status_code == 503
    quota.assert_not_awaited()
    upload.assert_not_awaited()
    submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"reference_video": "private-source"},
    {"reference_audio": reference_audio(17), "frames": 9},
    {"reference_audio": reference_audio(), "model": "LTX-2.5 Dev BF16"},
    {"guide_strength": 0.5},
])
async def test_bad_source_modes_reject_before_quota_upload_or_dispatch(setup, monkeypatch, values):
    app, submit, quota = setup
    upload = AsyncMock()
    monkeypatch.setattr(video_sources, "upload_sources", upload)
    response = await post(app, values)
    assert response.status_code == 422, response.text
    assert "private-source" not in response.text
    quota.assert_not_awaited()
    upload.assert_not_awaited()
    submit.assert_not_awaited()
