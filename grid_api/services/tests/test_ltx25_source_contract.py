# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from pathlib import Path

import pytest

from grid_api.services import recipes, video_contract
from grid_api.services.recipe_import import recipe_root, validate_recipe


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (Path(__file__).resolve().parents[3] / "recipes/qualification").glob("*.json"):
        workflow = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(workflow), workflow["_grid"]["name"], workflow)


@pytest.mark.parametrize("mode", ["audio", "video"])
@pytest.mark.parametrize("image", [False, True])
def test_governed_source_modes_freeze_audio_and_separate_opening_image(mode, image):
    inputs = {"prompt": "a red ball", "frames": 9, "width": 256, "height": 256, "source_mode": mode, "seed": 0}
    if image:
        inputs["keyframes"] = [{"frame": 0, "strength": 0.5}]
    resolved = recipes.resolve_for_model("LTX-2.5 Distilled BF16", inputs, has_source=image)
    graph = resolved["spec"]
    assert graph["latent_audio"]["class_type"] == "SetLatentNoiseMask"
    assert graph["audio_mask"]["inputs"]["value"] == 0
    assert graph["video"]["inputs"]["audio"] == ["source_audio", 0]
    assert "decode_audio" not in graph
    assert resolved["execution"]["source_mode"] == mode
    assert validate_recipe({"_grid": {"vars": {}}, **graph}) == []
    assert len(resolved["source_bindings"]) == (2 if mode == "video" else 1)
    if mode == "video":
        guide = graph["video_guide"]["inputs"]
        assert guide["latent"] == (["first_0", 0] if image else ["latent_video", 0])
        assert guide["latent_downscale_factor"] == ["control_model", 1]
        assert guide["strength"] == 0.85
    if not image:
        assert "first_0" not in graph
        assert "key_0" not in graph


def test_unqualified_recipe_cannot_acquire_source_mode_by_caller_claim():
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model("LTX-2.5 Dev BF16", {"source_mode": "audio"})
    with pytest.raises(recipes.RecipeError):
        recipes.resolve_for_model("LTX-2.5 Distilled BF16", {"guide_strength": 0.5})


def test_control_strength_is_explicit_and_does_not_change_opening_image_strength():
    result = recipes.resolve_for_model("LTX-2.5 Distilled BF16", {
        "source_mode": "video", "guide_strength": 0.7, "keyframes": [{"frame": 0, "strength": 0.2}],
    }, has_source=True)
    assert result["spec"]["video_guide"]["inputs"]["strength"] == 0.7
    assert result["spec"]["first_0"]["inputs"]["strength"] == 0.2


def test_source_expansion_refuses_non_governed_scaffold():
    with pytest.raises(ValueError):
        video_contract.expand_sources({}, "video")
