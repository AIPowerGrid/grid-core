# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dark Ingredients protocol/compiler proof, not output or identity qualification."""

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import ValidationError

from grid_api.config import GridSettings
from grid_api.routers import openai, video_jobs, videos
from grid_api.services import credits, generation_admission, pricing, recipes, storage, video_contract, video_sources
from grid_api.services.recipe_import import recipe_root, validate_recipe
from grid_api.services.tests.test_video_sources import wav
from grid_api.services.video_plans import compile_video

ROOT = Path(__file__).resolve().parents[3]
MODEL = "LTX-2.5 Dev BF16 Ingredients"


def png(size=(768, 448), fmt="PNG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, fmt)
    return output.getvalue()


def inline(raw):
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        workflow = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(workflow), workflow["_grid"]["name"], workflow)
    settings = GridSettings(_env_file=None, generation_enabled_paths=[
        "video", "audio-to-video", "video-to-video", "appearance-to-video",
    ])
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)
    monkeypatch.setattr(videos, "get_available_models", AsyncMock(return_value=[MODEL, "LTX-2.5 Distilled BF16"]))
    monkeypatch.setattr(videos.loras_svc, "prepare_loras", lambda *_args: [])
    monkeypatch.setattr(storage, "private_source_bucket", lambda: "private-synthetic-source")


@pytest.mark.parametrize("mode", ["audio", "video"])
def test_appearance_is_an_independent_guide_not_an_opening_pin(mode):
    recipe = recipes.recipes_for_model(MODEL)[0]
    original = copy.deepcopy(recipe.spec)
    result = recipes.resolve_for_model(MODEL, {"source_mode": mode, "reference_image": True, "seed": 0})
    graph, execution = result["spec"], result["execution"]
    assert validate_recipe({"_grid": {}, **graph}) == []
    assert recipe.spec == original
    assert result["image_paths"] is None and "image_bindings" not in result
    assert "LTXVImgToVideoInplace" not in recipes.node_types(graph)
    assert execution["appearance_profile"] == "ingredients-dev-v1"
    assert (execution["width"], execution["height"], execution["frames"], execution["steps"], execution["cfg"]) == (768, 448, 121, 40, 2)
    assert execution["seconds"] == 121 / 24
    assert graph["reference_sheet"]["inputs"]["amount"] == 121
    assert graph["reference_guide"]["inputs"]["latent"] == ["latent_video", 0]
    assert graph["video"]["inputs"]["audio"] == ["source_audio", 0]
    assert graph["audio_mask"]["inputs"]["value"] == 0
    assert result["source_bindings"][0] == {"role": "reference_image", "path": "reference_image.inputs.image"}
    if mode == "video":
        assert graph["control_model"]["inputs"]["model"] == ["reference_model", 0]
        assert graph["video_guide"]["class_type"] == "LTXAddVideoICLoRAGuideAdvanced"
        assert graph["video_guide"]["inputs"]["attention_strength"] == 0.25
        assert graph["video_guide"]["inputs"]["strength"] == 0.5
        assert graph["video_guide"]["inputs"]["latent"] == ["reference_guide", 2]
        assert graph["crop"]["inputs"]["positive"] == ["video_guide", 0]
    else:
        assert "motion_attention_strength" not in execution
        assert graph["crop"]["inputs"]["positive"] == ["reference_guide", 0]


@pytest.mark.parametrize("change", [
    {"reference_image": False}, {"source_mode": "generated"}, {"image": "portrait.png"},
    {"keyframes": [{"frame": 0}]}, {"frames": 81}, {"frames": 337},
    {"width": 448, "height": 768}, {"steps": 8}, {"cfg": 1},
    {"reference_strength": 0}, {"reference_strength": float("nan")},
    {"motion_attention_strength": 0}, {"motion_attention_strength": float("inf")},
])
def test_unqualified_profile_variations_reject_without_echoing_inputs(change):
    with pytest.raises(recipes.RecipeError, match="Invalid LTX-2.5 inputs"):
        recipes.resolve_for_model(MODEL, {"source_mode": "video", "reference_image": True, **change})


@pytest.mark.parametrize("change", [{"variant": "distilled"}, {"max_frames": 337}, {"max_pixels": 522240},
                                   {"max_keyframes": 1}, {"source_modes": ["audio"]}])
def test_metadata_cannot_widen_the_qualified_profile(change):
    contract = recipes.video_contract_for_model(MODEL)
    with pytest.raises(ValidationError):
        video_contract.LtxVideoContract.model_validate({**contract.model_dump(by_alias=True), **change})


@pytest.mark.parametrize("model", ["LTX-2.5 Distilled BF16", "LTX-2.5 Dev BF16"])
def test_appearance_never_falls_back_to_standard_recipes(model):
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model(model, {"reference_image": True})
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model(model, {"reference_strength": 0.5})


@pytest.mark.parametrize("raw", [b"not-an-image", png((448, 768)), png(fmt="JPEG"), png()[:64]])
def test_appearance_bytes_are_decoded_as_the_exact_png_not_assumed_from_mime(raw):
    with pytest.raises(HTTPException) as error:
        video_sources.validate_image(raw)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_validated_plan_binds_appearance_bytes_and_requires_new_worker_feature():
    raw = png()
    inputs = await videos.validate_video(videos.VideoRequest(
        prompt="synthetic test", model=MODEL, seed=0,
        reference_image=inline(raw), reference_audio=inline(wav(121)),
    ), {}, async_submission=True)
    assert not inputs.images
    plan = compile_video(inputs)
    payload = plan.payload
    assert "source_image_url" not in payload
    assert set(payload["required_media_features"]) == {
        "async-video-resume-v1", "recipe-media-sources-v1", "recipe-appearance-reference-v1",
    }
    assert payload["source_media"][0]["role"] == "reference_image"
    assert payload["source_media"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["recipe_source_bindings"][0]["path"] == "reference_image.inputs.image"
    assert payload["appearance_profile"] == plan.snapshot["execution"]["appearance_profile"] == "ingredients-dev-v1"
    assert payload["frames"] == 121 and payload["steps"] == 40


@pytest.mark.asyncio
async def test_default_dark_gate_rejects_before_decoder_quota_or_upload(monkeypatch):
    settings = GridSettings(_env_file=None, generation_enabled_paths=["video", "audio-to-video"])
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)
    decoder = AsyncMock(side_effect=AssertionError("must not decode"))
    monkeypatch.setattr(video_sources, "validate_sources", decoder)
    with pytest.raises(HTTPException) as error:
        await videos.validate_video(videos.VideoRequest(prompt="synthetic", model=MODEL,
                                   reference_image=inline(png()), reference_audio=inline(wav(121))), {})
    assert error.value.status_code == 503
    decoder.assert_not_awaited()


@pytest.mark.parametrize("change", [{"role": "reference_audio"}, {"width": 448}, {"bytes": True},
                                   {"content_type": "image/jpeg"}, {"frames": 121}, {"sha256": "x"}])
def test_appearance_commitments_cannot_be_relabelled_or_forged(change):
    execution = recipes.resolve_for_model(MODEL, {"reference_image": True, "source_mode": "audio"})
    sources = [{**source.commitment, "url": "https://source.invalid/test"} for source in (
        video_sources.validate_image(png()), video_sources.validate_audio(wav(121), 121),
    )]
    video_sources.validate_commitments(sources, execution["source_bindings"], execution["execution"])
    sources[0].update(change)
    with pytest.raises(HTTPException):
        video_sources.validate_commitments(sources, execution["source_bindings"], execution["execution"])


def test_intent_binds_appearance_separately_and_all_quality_controls():
    base = dict(prompt="synthetic", model=MODEL, max_cost_micro_usd=1,
                reference_audio=inline(wav(121)), reference_image=inline(png()))
    original = video_jobs.request_hash(video_jobs.AsyncVideoRequest(**base))
    equivalent = {**base, "reference_image": "data:image/png;base64," + base["reference_image"]}
    assert video_jobs.request_hash(video_jobs.AsyncVideoRequest(**equivalent)) == original
    for change in ({"reference_image": inline(png(color="blue"))}, {"reference_strength": 0.5},
                   {"motion_attention_strength": 0.5}):
        assert video_jobs.request_hash(video_jobs.AsyncVideoRequest(**{**base, **change})) != original


@pytest.mark.asyncio
async def test_authenticated_inventory_lists_ingredients_and_advanced_control_without_mutating_base(monkeypatch):
    monkeypatch.setattr(videos.accounts_svc, "authenticate", AsyncMock(return_value={}))
    recipe = recipes.recipes_for_model(MODEL)[0]
    original = copy.deepcopy(recipe.spec)
    app = FastAPI()
    app.include_router(openai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/models/{MODEL}/recipe", headers={"apikey": "test-key"})
    assert response.status_code == 200
    record = response.json()["recipes"][0]
    assert record["spec"] == original == recipe.spec
    for mode in ("audio", "video"):
        assert "ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors" in record["source_requirements"][mode]["model_files"]
        assert "RepeatImageBatch" in record["source_requirements"][mode]["node_types"]
    assert "LTXAddVideoICLoRAGuideAdvanced" in record["source_requirements"]["video"]["node_types"]


def test_new_profile_stays_non_serving_unpriced_and_exposes_exact_controls():
    assert not pricing.is_priced_for(MODEL, "video")
    params = recipes.param_schema(MODEL)["params"]
    assert params["reference_image"]["width"] == 768
    assert params["frames"]["minimum"] == params["frames"]["maximum"] == 121
    assert params["steps"]["minimum"] == params["steps"]["maximum"] == 40
    recipes.load_local_recipes(str(ROOT / "recipes"))
    assert MODEL not in {recipe.model_name for recipe in recipes._LOCAL_RECIPES.values()}


@pytest.mark.asyncio
async def test_live_money_gate_rejects_unpriced_profile_before_reserving(monkeypatch):
    monkeypatch.setattr(credits, "charging_enabled_for", lambda *_args: True)
    result = await credits.authorize_media(None, MODEL, "video", 1, 121 / 24, "synthetic", record_reservation=True)
    assert result["ok"] is False and result["status"] == "unpriced" and result["reserved"] == 0


def test_animated_or_rotated_appearance_images_reject():
    for animated in (False, True):
        output = io.BytesIO()
        image = Image.new("RGB", (768, 448), "red")
        if animated:
            image.save(output, "PNG", save_all=True, append_images=[Image.new("RGB", image.size, "blue")])
        else:
            exif = Image.Exif()
            exif[274] = 6
            image.save(output, "PNG", exif=exif)
        with pytest.raises(HTTPException) as error:
            video_sources.validate_image(output.getvalue())
        assert error.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["audio", "video"])
async def test_core_payload_reaches_actual_worker_appearance_adapter(monkeypatch, tmp_path, mode):
    """Optional joint protocol check; mocked HTTP is not a native GPU/R2 canary."""
    source = os.environ.get("MEDIA_VIDEO_WORKER_SOURCE")
    if not source:
        pytest.skip("reviewed MEDIA_VIDEO_WORKER_SOURCE is required for the joint adapter check")
    worker = Path(source).resolve()
    python = worker / ".venv/bin/python"
    if not python.is_file() or not (worker / "bridge/source_media.py").is_file():
        pytest.fail("joint adapter check requires a reviewed worker checkout and its test runtime")
    body = {"prompt": "synthetic test", "model": MODEL, "seed": 0,
            "reference_image": inline(png()), "reference_audio": inline(wav(121))}
    if mode == "video":
        decoder = shutil.which("ffmpeg")
        if not decoder or not shutil.which("ffprobe"):
            pytest.fail("joint video adapter check requires the independent FFmpeg decoder")
        path = tmp_path / "synthetic.mp4"
        subprocess.run([decoder, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=768x448:rate=24",
                        "-frames:v", "121", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path)],
                       capture_output=True, timeout=20, check=True)
        body.update(reference_video=inline(path.read_bytes()), size="768x448")
    inputs = await videos.validate_video(videos.VideoRequest(**body), {}, async_submission=True)
    plan = compile_video(inputs)
    content = {}

    def stage(raw, extension):
        url = "https://" + "a" * 32 + f".r2.cloudflarestorage.com/synthetic/{len(content)}.{extension}"
        content[url] = inline(raw)
        return url

    monkeypatch.setattr(storage, "upload_private_source", stage)
    payload = await plan.stage()
    script = """
import asyncio, base64, copy, json, sys
import httpx, respx
sys.path.insert(0, sys.argv[1])
from bridge import source_media, workflow
source_media.Settings.COMFYUI_URL = "http://127.0.0.1:8188"
request = json.load(sys.stdin)
body = request["payload"]
original = copy.deepcopy(body)
uploaded = []
def upload(request):
    assert "authorization" not in request.headers
    name = "bound-" + str(len(uploaded)) + "." + uploaded[-1].split(".")[-1]
    return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
def read(request):
    assert "authorization" not in request.headers
    url = str(request.url)
    uploaded.append(url)
    return httpx.Response(200, content=base64.b64decode(request_data[url]))
request_data = request["content"]
async def run():
    with respx.mock(assert_all_called=True) as routes:
        for url in request_data:
            routes.get(url).mock(side_effect=read)
        routes.post("http://127.0.0.1:8188/upload/image").mock(side_effect=upload)
        graph = await workflow.build_recipe_workflow({"id": "synthetic"}, body)
        assert body == original
        assert len(uploaded) == len(request_data)
        assert all(node.get("class_type") != "LTXVImgToVideoInplace" for node in graph.values())
        for index, binding in enumerate(body["recipe_source_bindings"]):
            node, _, field = binding["path"].split(".")
            extension = {"reference_image": "png", "reference_audio": "wav", "reference_video": "mp4"}[binding["role"]]
            assert graph[node]["inputs"][field] == "bound-" + str(index + 1) + "." + extension
        assert graph["video"]["inputs"]["audio"] == ["source_audio", 0]
        assert graph["reference_guide"]["inputs"]["latent"] == ["latent_video", 0]
        if "source_video" in graph:
            assert graph["control_model"]["inputs"]["model"] == ["reference_model", 0]
            assert graph["video_guide"]["inputs"]["latent"] == ["reference_guide", 2]
asyncio.run(run())
"""
    result = subprocess.run([str(python), "-c", script, str(worker)], cwd=tmp_path,
                            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                                 "PYTHONNOUSERSITE": "1", "PYTHON_DOTENV_DISABLED": "1"},
                            input=json.dumps({"payload": payload, "content": content}),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, "joint appearance adapter failed on synthetic inputs"
