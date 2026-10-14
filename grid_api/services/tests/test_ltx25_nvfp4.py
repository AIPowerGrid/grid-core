# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Distinct bounded-pilot NVFP4 contracts; fixtures are not native qualification."""

import copy
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI
from fastapi import HTTPException

from grid_api.routers import openai
from grid_api.config import GridSettings
from grid_api.services import credits, generation_admission, recipes
from grid_api.services.recipe_import import recipe_root, validate_recipe

ROOT = Path(__file__).resolve().parents[3]
MODEL = "LTX-2.5 Distilled NVFP4"
CHECKPOINT = "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        workflow = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(workflow), workflow["_grid"]["name"], workflow)


@pytest.mark.parametrize("mode", ["t2v", "i2v"])
def test_only_distinct_checkpoint_and_identity_change(mode):
    path = ROOT / "recipes/qualification"
    bf16 = json.loads((path / f"ltx-2.5-distilled-bf16-{mode}-v1.json").read_text())
    nvfp4 = json.loads((path / f"ltx-2.5-distilled-nvfp4-{mode}-v1.json").read_text())
    expected = copy.deepcopy(bf16)
    expected["_grid"].update(name=f"{MODEL} {mode} v1", modelName=MODEL, requiredModels=[MODEL])
    expected["model"]["inputs"]["unet_name"] = CHECKPOINT
    assert nvfp4 == expected
    assert validate_recipe(nvfp4) == []
    assert recipe_root(nvfp4) != recipe_root(bf16)
    promoted = json.loads((ROOT / "recipes" / f"ltx-2.5-distilled-nvfp4-{mode}-v1.json").read_text())
    assert promoted == nvfp4


@pytest.mark.parametrize("model", [MODEL, "LTX-2.5 Distilled BF16"])
@pytest.mark.parametrize("mode", ["pilot", "open"])
def test_startup_inventory_cannot_enable_admission_or_waiting_routes(monkeypatch, model, mode):
    recipes.load_local_recipes(str(ROOT / "recipes"))
    assert MODEL in {record.model_name for record in recipes._LOCAL_RECIPES.values()}
    settings = GridSettings(_env_file=None, media_async_enabled=False, media_async_admission_mode=mode)
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as denied:
        generation_admission.require_async_video(settings, "00000000-0000-4000-8000-000000000001")
    assert denied.value.status_code == 503
    with pytest.raises(HTTPException) as waiting:
        generation_admission.require_waiting_video(model)
    assert waiting.value.status_code == 503


@pytest.mark.parametrize("model", [MODEL, "LTX-2.5 Distilled BF16"])
@pytest.mark.parametrize("enabled,waiting,admitted", [(True, False, False), (True, True, True), (False, True, False)])
def test_public_async_does_not_silently_open_typed_waiting(monkeypatch, model, enabled, waiting, admitted):
    settings = GridSettings(_env_file=None, media_async_enabled=enabled,
                            media_async_admission_mode="open", typed_video_waiting_enabled=waiting)
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)
    if admitted:
        assert generation_admission.require_waiting_video(model) is None
    else:
        with pytest.raises(HTTPException) as denied:
            generation_admission.require_waiting_video(model)
        assert denied.value.status_code == 503
    assert generation_admission.require_waiting_video("LTX-2.3") is None
    if enabled:
        assert generation_admission.require_async_video(settings, "00000000-0000-4000-8000-000000000001") is None


@pytest.mark.parametrize("model", [MODEL, "LTX-2.5 Dev BF16", "unknown-video"])
def test_public_async_accepts_only_reviewed_bf16(model):
    settings = GridSettings(_env_file=None, media_async_enabled=True, media_async_admission_mode="open")
    with pytest.raises(HTTPException) as denied:
        generation_admission.require_async_video(settings, "00000000-0000-4000-8000-000000000001", model=model)
    assert denied.value.status_code == 503


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("source_mode", ["generated", "audio", "video"])
@pytest.mark.parametrize("frames", [33, 81, 337])
def test_execution_and_sources_cannot_fall_back_to_bf16(image, source_mode, frames):
    values = {"prompt": "synthetic scene", "frames": frames, "seed": 0, "source_mode": source_mode}
    if source_mode == "video":
        values["height"] = 512
    if image:
        values["keyframes"] = [{"frame": 0, "strength": 0.5}]
    result = recipes.resolve_for_model(MODEL, values, has_source=image)
    graph = result["spec"]
    assert graph["model"]["inputs"]["unet_name"] == CHECKPOINT
    assert graph["encoder"]["inputs"]["clip_name"] == "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
    assert result["execution"]["frames"] == frames
    assert result["execution"]["seconds"] == frames / 24
    assert result["execution"]["steps"] == 8
    assert result["execution"]["cfg"] == 1
    assert graph["video"]["inputs"]["audio"] == (["decode_audio", 0] if source_mode == "generated" else ["source_audio", 0])
    if source_mode == "video":
        assert graph["control_model"]["inputs"]["model"] == ["model", 0]
        assert graph["control_model"]["inputs"]["lora_name"] == "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors"
    if source_mode != "generated":
        assert len(result["source_bindings"]) == (2 if source_mode == "video" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("frames,cost", [(33, 27_500), (81, 67_500), (337, 280_834)])
async def test_exact_frame_quote_stays_in_distinct_nvfp4_identity(monkeypatch, frames, cost):
    monkeypatch.setattr(credits, "account_credit_summary", AsyncMock(return_value={
        "paid": {"balance_micro": 1_000_000}, "free": {"active": False, "remaining_micro": 0},
        "promotional": {"active": False, "remaining_micro": 0},
    }))
    monkeypatch.setattr(credits, "holder_discount_bps", AsyncMock(return_value=0))
    result = await credits.quote_for_account({}, model=MODEL, modality="video", frames=frames)
    assert result["estimate"]["priced"] is True
    assert result["estimate"]["cost_micro"] == cost
    assert result["estimate"]["frames"] == frames


@pytest.mark.parametrize("values", [{"precision": "bf16"}, {"unet_name": CHECKPOINT}, {"steps": 12}, {"cfg": 2.0}])
def test_precision_and_weight_selection_are_not_public_knobs(values):
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model(MODEL, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [[], ["audio", "video"]])
async def test_actual_core_inventory_parses_in_reviewed_worker(monkeypatch, modes):
    """Actual cross-repo parsing, not GPU, storage or paid qualification."""
    source = os.environ.get("MEDIA_VIDEO_WORKER_SOURCE")
    if not source:
        pytest.skip("reviewed MEDIA_VIDEO_WORKER_SOURCE required for joint startup parsing")
    worker = Path(source).resolve()
    python = worker / ".venv/bin/python"
    if not python.is_file() or not (worker / "bridge/video_startup.py").is_file():
        pytest.fail("joint startup parsing requires reviewed worker source and test runtime")
    from grid_api.services import accounts
    monkeypatch.setattr(accounts, "authenticate", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(openai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/models/{MODEL}/recipe", headers={"apikey": "synthetic-key"})
    assert response.status_code == 200
    code = (
        "import json,sys; from bridge.video_startup import _recipe_requirements,NVFP4_MODEL; "
        "records=json.load(sys.stdin); modes=json.loads(sys.argv[1]); "
        "required=_recipe_requirements(records,NVFP4_MODEL,modes); "
        "print(json.dumps({'requirements':len(required)}))"
    )
    result = subprocess.run([str(python), "-c", code, json.dumps(modes)], cwd=worker,
                            input=response.text, capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(result.stdout) == {"requirements": 2 * (1 + len(modes))}
