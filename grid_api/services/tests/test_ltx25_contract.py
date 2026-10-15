# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from grid_api.config import GridSettings
from grid_api.services import credits, generation_admission, job_queue, media, pricing, recipes, token_stream, video_contract
from grid_api.services.recipe_import import recipe_root, validate_recipe

ROOT = Path(__file__).resolve().parents[3]
MODEL = "LTX-2.5 Distilled BF16"


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(generation_admission, "get_settings", lambda: GridSettings(
        _env_file=None, media_async_enabled=True, media_async_admission_mode="open",
        typed_video_waiting_enabled=True))
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        workflow = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(workflow), workflow["_grid"]["name"], workflow)
    monkeypatch.setattr(generation_admission, "require_media", lambda *_args: None)


def test_startup_loads_only_promoted_distilled_recipes():
    recipes.load_local_recipes(str(ROOT / "recipes"))
    names = {recipe.model_name for recipe in recipes._LOCAL_RECIPES.values()}
    assert MODEL in names
    assert "LTX-2.5 Dev BF16" not in names
    assert "LTX-2.5 Dev BF16 Ingredients" not in names
    assert "LTX-2.5 Distilled NVFP4" in names
    for mode in ("t2v", "i2v"):
        filename = f"ltx-2.5-distilled-bf16-{mode}-v1.json"
        promoted = json.loads((ROOT / "recipes" / filename).read_text())
        qualified = json.loads((ROOT / "recipes/qualification" / filename).read_text())
        assert promoted == qualified
        assert validate_recipe(promoted) == []


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
    assert (
        graph["conditioning"]["inputs"]["frame_rate"]
        == graph["latent_audio"]["inputs"]["frame_rate"]
        == graph["video"]["inputs"]["fps"] == 24
    )
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
    assert schema["params"]["frames"]["maximum"] == 481
    assert schema["params"]["keyframes"]["max_items"] == 16
    assert schema["video_contract"]["schema"] == "aipg.ltx-video.v1"
    assert schema["video_contract"]["max_keyframes"] == 16


def test_text_only_recipe_does_not_advertise_keyframe_inputs():
    schema = recipes.param_schema(f"{MODEL} t2v v1")
    assert schema["capabilities"]["img2img"] is False
    assert "image" not in schema["params"]
    assert "keyframes" not in schema["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timing,expected", [({"seconds": 4.0}, 97), ({"frames": 297}, 297)])
async def test_quote_reports_effective_duration_even_when_not_priced(monkeypatch, timing, expected):
    monkeypatch.delitem(pricing.PRICING, MODEL.lower())
    monkeypatch.setattr(credits, "account_credit_summary", AsyncMock(return_value={}))
    result = await credits.quote_for_account({}, model=MODEL, modality="video", **timing)
    assert result["estimate"]["seconds"] == expected / 24
    assert result["estimate"]["frames"] == expected
    assert result["estimate"]["priced"] is False  # Pricing is a separate activation gate.


@pytest.mark.asyncio
@pytest.mark.parametrize("frames,cost", [(81, 67_500), (97, 80_834), (337, 280_834), (385, 320_834), (481, 400_834)])
async def test_approved_tariff_quotes_exact_frame_cost_without_debit(monkeypatch, frames, cost):
    summary = {
        "paid": {"balance_micro": 1_000_000},
        "promotional": {"active": False, "remaining_micro": 0},
        "free": {"active": False, "remaining_micro": 0},
    }
    original = copy.deepcopy(summary)
    monkeypatch.setattr(credits, "account_credit_summary", AsyncMock(return_value=summary))
    monkeypatch.setattr(credits, "holder_discount_bps", AsyncMock(return_value=0))
    result = await credits.quote_for_account({}, model=MODEL, modality="video", frames=frames)
    estimate = result["estimate"]
    assert estimate["priced"] is True
    assert estimate["frames"] == frames
    assert estimate["seconds"] == frames / 24
    assert estimate["base_cost_micro"] == estimate["cost_micro"] == cost
    assert estimate["from_paid_micro"] == cost
    assert estimate["shortfall_micro"] == 0
    for pocket in ("paid", "promotional", "free"):
        assert result[pocket] == original[pocket]


@pytest.mark.parametrize("source", [False, True])
def test_long_bf16_clip_preserves_exact_graph_units_and_late_pin(source):
    values = {"frames": 481, "seed": 0}
    if source:
        values["keyframes"] = [{"frame": 0, "strength": 1.0}, {"frame": 480, "strength": 0.5}]
    resolved = recipes.resolve_for_model(MODEL, values, has_source=source)
    assert resolved["execution"]["frames"] == 481
    assert resolved["execution"]["seconds"] == 481 / 24
    assert resolved["spec"]["latent_audio"]["inputs"]["frames_number"] == 481
    assert resolved["spec"]["latent_video"]["inputs"]["length"] == 481
    if source:
        assert resolved["spec"]["guide_1"]["inputs"]["frame_idx"] == 480


@pytest.mark.parametrize("model,frames", [(MODEL, 489), (MODEL, 480),
                                         ("LTX-2.5 Distilled NVFP4", 345), ("LTX-2.5 Dev BF16", 89)])
def test_duration_extension_does_not_widen_other_models_or_round_frames(model, frames):
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model(model, {"frames": frames})


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


@pytest.mark.parametrize("variant,frames,positions,strengths", [
    ("distilled", 297, [0, 42, 72, 96, 125, 154, 190, 202, 226, 250], [1.0] * 10),
    ("distilled", 337, [0, 56, 113, 145, 196, 244, 281], [1.0] * 7),
    ("dev", 33, [0, 32], [1.0, 1.0]),
    ("distilled", 81, [0, 14, 80], [1.0, 0.5, 1.0]),
])
def test_governed_expansion_carries_handoff_timed_pins(variant, frames, positions, strengths):
    model = f"LTX-2.5 {'Distilled' if variant == 'distilled' else 'Dev'} BF16"
    pins = [{"frame": frame, "strength": strength} for frame, strength in zip(positions, strengths)]
    resolved = recipes.resolve_for_model(model, {"frames": frames, "keyframes": pins, "seed": 0,
                                               "width": 768, "height": 512}, has_source=True)
    graph = resolved["spec"]
    assert resolved["execution"]["keyframes"] == pins
    assert resolved["execution"]["seconds"] == frames / 24
    assert graph["first_0"]["inputs"]["strength"] == strengths[0]
    assert resolved["image_bindings"] == [
        {"image_index": index, "path": f"key_{index}.inputs.image"} for index in range(len(pins))]
    for index, pin in enumerate(pins[1:], 1):
        assert graph[f"guide_{index}"]["inputs"]["frame_idx"] == pin["frame"]
        assert graph[f"guide_{index}"]["inputs"]["strength"] == pin["strength"]
        assert graph[f"guide_{index}"]["inputs"]["latent"] == (["first_0", 0] if index == 1 else [f"guide_{index - 1}", 2])
        assert graph[f"scale_{index}"]["inputs"]["width"] == 768
        assert graph[f"scale_{index}"]["inputs"]["height"] == 512
    final = f"guide_{len(pins) - 1}"
    assert graph["av_latent"]["inputs"]["video_latent"] == [final, 2]
    assert graph["guider"]["inputs"]["positive"] == graph["crop"]["inputs"]["positive"] == [final, 0]
    assert graph["guider"]["inputs"]["negative"] == graph["crop"]["inputs"]["negative"] == [final, 1]
    if variant == "dev":
        assert graph["sigmas"]["inputs"]["latent"] == [final, 2]
    # Resolving another request must not mutate the canonical recipe.
    fresh = recipes.resolve_for_model(model, {}, has_source=True)["spec"]
    assert "guide_1" not in fresh
    assert fresh["first_0"]["inputs"]["strength"] == 1


@pytest.mark.parametrize("pins", [
    [{"frame": 1}], [{"frame": 0}, {"frame": 0}],
    [{"frame": 0}, {"frame": 32}, {"frame": 16}], [{"frame": 0}, {"frame": 81}],
    [{"frame": True}], [{"frame": 0, "strength": float("nan")}],
    [{"frame": 0, "strength": -0.1}], [{"frame": 0, "strength": 1.1}],
    [{"frame": 0, "path": "arbitrary.inputs.file"}],
    [{"frame": index} for index in range(17)],
])
def test_invalid_timed_pins_reject_at_recipe_boundary(pins):
    with pytest.raises(recipes.RecipeError, match="Invalid LTX-2.5 inputs"):
        recipes.resolve_for_model(MODEL, {"keyframes": pins}, has_source=True)


def test_text_only_recipe_cannot_accept_pins():
    with pytest.raises(recipes.RecipeError, match="Invalid LTX-2.5 inputs"):
        recipes.resolve_for_model(MODEL, {"keyframes": [{"frame": 0}]}, has_source=False)


@pytest.mark.parametrize("sources", [[], "not-a-list", [None], ["other"], ["first"] * 17])
@pytest.mark.asyncio
async def test_malformed_source_list_rejects_before_money(monkeypatch, sources):
    reserve = AsyncMock()
    monkeypatch.setattr(credits, "authorize_media", reserve)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(MODEL, "video", {"source_image_urls": sources, "source_image_url": "first"}, 1)
    assert exc.value.status_code == 422
    reserve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [["first"], ["first", "last"]])
async def test_pin_sources_and_worker_compatibility_checked_before_money(monkeypatch, sources):
    from grid_api.routers import worker_ws
    reserve = AsyncMock()
    monkeypatch.setattr(credits, "authorize_media", reserve)
    available = AsyncMock(return_value=[])
    monkeypatch.setattr(worker_ws, "get_available_models", available)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(MODEL, "video", {
            "source_image_url": "first", "source_image_urls": sources,
            "recipe_inputs": {"keyframes": [{"frame": 0}, {"frame": 80}]},
        }, 1)
    assert exc.value.status_code == (422 if len(sources) == 1 else 503)
    reserve.assert_not_awaited()


@pytest.mark.parametrize("variant", ["distilled", "dev"])
def test_broken_governed_scaffold_rejects_instead_of_skipping_references(variant):
    model = f"LTX-2.5 {'Distilled' if variant == 'distilled' else 'Dev'} BF16"
    recipe = next(recipe for recipe in recipes.recipes_for_model(model) if "image" in recipe.vars)
    del recipe.spec["video_vae"]
    with pytest.raises(recipes.RecipeError, match="scaffold is unavailable"):
        recipes.resolve_for_model(model, {"keyframes": [{"frame": 0}, {"frame": 80}]}, has_source=True)


def test_total_image_bounds_reject_before_decoding(monkeypatch):
    calls = []
    monkeypatch.setattr(media, "decode_source_image", lambda value: calls.append(value))
    with pytest.raises(HTTPException) as exc:
        media.decode_video_keyframes(["x" * (17 * 1024 * 1024)] * 2)
    assert exc.value.status_code == 413
    assert not calls


def test_total_decoded_image_bound_rejects(monkeypatch):
    monkeypatch.setattr(media, "decode_source_image", lambda _value: (b"x" * (9 * 1024 * 1024), "png", 32, 32))
    with pytest.raises(HTTPException) as exc:
        media.decode_video_keyframes(["a", "b", "c"])
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_reused_reference_image_uploads_once(monkeypatch):
    uploads = []
    def upload(raw, ext):
        uploads.append((raw, ext))
        return f"https://source.invalid/{len(uploads)}.png"
    monkeypatch.setattr(media.storage, "upload_private_source", upload)
    urls = await media.upload_video_keyframes([(b"a", "png", 32, 32), (b"b", "png", 32, 32), (b"a", "png", 32, 32)])
    assert urls == ["https://source.invalid/1.png", "https://source.invalid/2.png", "https://source.invalid/1.png"]
    assert len(uploads) == 2


@pytest.mark.asyncio
async def test_available_models_filters_old_workers_for_timed_images(monkeypatch):
    from grid_api.routers import worker_ws
    class Pipeline:
        def get(self, _key):
            return self
        async def execute(self):
            return [json.dumps({"job_types": ["video"], "models": ["old-only"]}),
                    json.dumps({"job_types": ["video"], "models": [MODEL], "media_features": ["recipe-image-bindings-v1"]}),
                    json.dumps({"job_types": ["image"], "models": ["image-only"], "media_features": ["recipe-image-bindings-v1"]})]
    class Redis:
        async def smembers(self, _key):
            return ["old", "new", "image"]
        def pipeline(self):
            return Pipeline()
    monkeypatch.setattr(worker_ws, "get_redis", lambda: Redis())
    ordinary = await worker_ws.get_available_models(job_type="video")
    assert "old-only" in ordinary and MODEL in ordinary
    compatible = await worker_ws.get_available_models(job_type="video", media_feature="recipe-image-bindings-v1")
    assert MODEL in compatible and "old-only" not in compatible and "image-only" not in compatible
