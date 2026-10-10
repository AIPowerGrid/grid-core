# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from grid_api.services import credits, generation_admission, job_queue, media, recipes, token_stream, video_contract
from grid_api.services.recipe_import import recipe_root, validate_recipe

ROOT = Path(__file__).resolve().parents[3]
MODEL = "LTX-2.5 Distilled BF16"


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        workflow = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(workflow), workflow["_grid"]["name"], workflow)
    monkeypatch.setattr(generation_admission, "require_media", lambda *_args: None)


def test_qualification_directory_is_not_loaded_at_startup():
    recipes.load_local_recipes(str(ROOT / "recipes"))
    assert MODEL not in {recipe.model_name for recipe in recipes._LOCAL_RECIPES.values()}


@pytest.mark.parametrize("variant", ["distilled", "dev"])
@pytest.mark.parametrize("source", [False, True])
def test_recipe_structure_and_exact_execution_units(variant, source):
    model = f"LTX-2.5 {'Distilled' if variant == 'distilled' else 'Dev'} BF16"
    resolved = recipes.resolve_for_model(model, {"prompt": "a red ball", "frames": 81, "seed": 0}, has_source=source)
    graph = resolved["spec"]
    expected_steps = 8 if variant == "distilled" else 40
    assert resolved["seed"] == graph["noise"]["inputs"]["noise_seed"] == 0
    assert resolved["execution"] == {
        "width": 960, "height": 544, "frames": 81, "fps": 24, "seconds": 81 / 24,
        "steps": expected_steps, "cfg": 1.0 if variant == "distilled" else 2.0,
        "sampler": "euler_ancestral_cfg_pp", "audio": True,
    }
    assert graph["latent_video"]["inputs"]["length"] == graph["latent_audio"]["inputs"]["frames_number"] == 81
    assert graph["conditioning"]["inputs"]["frame_rate"] == graph["latent_audio"]["inputs"]["frame_rate"] == graph["video"]["inputs"]["fps"] == 24
    assert graph["guider"]["inputs"]["cfg"] == resolved["execution"]["cfg"]
    assert graph["sampler"]["inputs"]["sampler_name"] == resolved["execution"]["sampler"]
    assert graph["video"]["inputs"]["audio"] == ["decode_audio", 0]
    assert graph["model"]["inputs"]["unet_name"] == f"ltx-2.5-22b-{variant}-transformer-bf16.safetensors"
    if source:
        assert resolved["image_paths"] == "key_0.inputs.image"
        assert graph["scale_0"]["inputs"]["width"] == 960
        assert graph["scale_0"]["inputs"]["height"] == 544
        assert graph["first_0"]["inputs"]["strength"] == 1
    else:
        assert "key_0" not in graph
    path = ROOT / "recipes/qualification" / f"ltx-2.5-{variant}-bf16-{'i2v' if source else 't2v'}-v1.json"
    assert validate_recipe(json.loads(path.read_text())) == []


def test_duration_is_explicit_and_never_silently_clamped():
    contract = recipes.video_contract_for_model(MODEL)
    assert video_contract.normalize(contract, {"seconds": 4.0})["frames"] == 97
    assert video_contract.normalize(contract, {"frames": 297})["seconds"] == 12.375
    assert video_contract.normalize(contract, {"frames": 81, "seconds": 3.375})["frames"] == 81
    with pytest.raises(ValueError):
        video_contract.normalize(contract, {"frames": 81, "seconds": 4.0})


@pytest.mark.parametrize("values", [
    {"frames": 96}, {"frames": 0}, {"frames": True}, {"frames": "81"},
    {"width": 1000}, {"width": 768, "height": 768}, {"width": 961},
    {"steps": 30}, {"cfg": 2.0}, {"fps": 30}, {"fps": 24.0}, {"audio": False}, {"audio": 1},
    {"seconds": float("nan")}, {"seconds": float("inf")}, {"seconds": 0.1},
    {"sampler": "euler"}, {"scheduler": "normal"}, {"seed": True}, {"seed": 1.5},
    {"seed": 2**53}, {"prompt": "x" * 8001}, {"precision": "int8"},
])
def test_invalid_units_reject_at_recipe_boundary_without_reflecting_inputs(values):
    with pytest.raises(recipes.RecipeError) as exc:
        recipes.resolve_for_model(MODEL, values)
    assert str(exc.value) == "Invalid LTX-2.5 inputs; consult the model parameter contract"


def test_dev_is_a_distinct_contract_with_a_smaller_workload_ceiling():
    model = "LTX-2.5 Dev BF16"
    result = recipes.resolve_for_model(model, {"steps": 12, "cfg": 3.0})
    assert result["execution"]["steps"] == result["spec"]["sigmas"]["inputs"]["steps"] == 12
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model(model, {"frames": 297})


def test_new_contract_never_falls_back_to_a_recipe_with_wrong_source_mode():
    with pytest.raises(recipes.RecipeError, match="source-image mode"):
        recipes.resolve_for_model(f"{MODEL} i2v v1", {}, has_source=False)
    with pytest.raises(recipes.RecipeError, match="source-image mode"):
        recipes.resolve_for_model(f"{MODEL} t2v v1", {}, has_source=True)


@pytest.mark.parametrize("size", [(4096, 512), (512, 4096), (1024, 1024), (1920, 1080), (100, 200)])
def test_source_auto_fit_stays_within_contract(size):
    contract = recipes.video_contract_for_model(MODEL, has_source=True)
    width, height = video_contract.source_size(contract, *size)
    assert width % 32 == height % 32 == 0
    assert width * height <= contract.max_pixels
    assert video_contract.normalize(contract, {"width": width, "height": height})["width"] == width


def test_parameter_schema_exposes_only_qualified_surface():
    schema = recipes.param_schema(MODEL)
    assert schema["params"]["size"]["multiple_of"] == 32
    assert schema["params"]["steps"]["minimum"] == schema["params"]["steps"]["maximum"] == 8
    assert schema["params"]["frames"]["maximum"] == 297
    assert schema["params"]["keyframes"]["max_items"] == 1
    assert schema["video_contract"]["schema"] == "aipg.ltx-video.v1"


def test_text_only_recipe_does_not_advertise_keyframe_inputs():
    schema = recipes.param_schema(f"{MODEL} t2v v1")
    assert schema["capabilities"]["img2img"] is False
    assert "image" not in schema["params"]
    assert "keyframes" not in schema["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timing,expected", [({"seconds": 4.0}, 97), ({"frames": 297}, 297)])
async def test_quote_reports_effective_duration_even_when_not_priced(monkeypatch, timing, expected):
    monkeypatch.setattr(credits, "account_credit_summary", AsyncMock(return_value={}))
    result = await credits.quote_for_account({}, model=MODEL, modality="video", **timing)
    assert result["estimate"]["seconds"] == expected / 24
    assert result["estimate"]["frames"] == expected
    assert result["estimate"]["priced"] is False  # Pricing is a separate activation gate.


@pytest.mark.asyncio
async def test_quote_rejects_conflicts_and_unsupported_frame_contract_before_db(monkeypatch):
    summary = AsyncMock()
    monkeypatch.setattr(credits, "account_credit_summary", summary)
    for model, values in ((MODEL, {"frames": 81, "seconds": 4.0}), (MODEL, {"frames": 81, "n": 2}),
                          ("LTX-2.3", {"frames": 81})):
        with pytest.raises(HTTPException) as exc:
            await credits.quote_for_account({}, model=model, modality="video", **values)
        assert exc.value.status_code == 422
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_video_rejects_before_reserve_or_dispatch(monkeypatch):
    reserve = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(credits, "authorize_media", reserve)
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(MODEL, "video", {"recipe_inputs": {"frames": 96}}, 1, account_id="test")
    assert exc.value.status_code == 422
    reserve.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, {"spec": {}}])
async def test_missing_graph_snapshot_rejects_before_reserve_or_dispatch(monkeypatch, resolved):
    reserve = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(recipes, "resolve_for_model", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(credits, "authorize_media", reserve)
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(MODEL, "video", {}, 1, account_id="test")
    assert exc.value.status_code == 503
    reserve.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reservation_and_dispatch_use_one_graph_snapshot(monkeypatch):
    observed = {}
    real_resolve = recipes.resolve_for_model
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert calls == 1, "the graph must not be re-resolved after reserving"
        return real_resolve(*args, **kwargs)

    async def authorize(_account, model, job_type, n, seconds, _job, **kwargs):
        observed["seconds"] = seconds
        assert kwargs["record_reservation"] is True
        return {"ok": True, "reserved": 1}

    async def dispatch(_job, payload, models, **kwargs):
        observed["payload"] = copy.deepcopy(payload)
        assert observed["seconds"] == payload["frames"] / payload["fps"]
        assert models == [MODEL]

    async def events(*_args, **_kwargs):
        yield {"text": token_stream.DONE_SENTINEL, "full_text": json.dumps({"media": [{"url": "https://media.invalid/video.mp4"}]})}

    monkeypatch.setattr(recipes, "resolve_for_model", resolve)
    monkeypatch.setattr(credits, "authorize_media", authorize)
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    monkeypatch.setattr(token_stream, "subscribe_tokens", events)
    _, meta = await media.submit_and_wait(MODEL, "video", {
        "prompt": "a red ball", "seed": 0, "n": 1, "steps": 30, "cfg_scale": 7.5,
        "recipe_inputs": {"seconds": 4.0},
    }, 1, account_id="test")
    payload = observed["payload"]
    assert payload["steps"] == meta["execution"]["steps"] == 8
    assert payload["cfg_scale"] == 1
    assert payload["frames"] == payload["length"] == payload["video_length"] == 97
    assert payload["recipe_spec"]["latent_video"]["inputs"]["length"] == 97
    assert payload["recipe_spec"]["latent_audio"]["inputs"]["frames_number"] == 97
    assert payload["seed"] == payload["recipe_spec"]["noise"]["inputs"]["noise_seed"] == 0
    assert meta["execution"]["seconds"] == 97 / 24
