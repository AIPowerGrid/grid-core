# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from fastapi import HTTPException

from grid_api.services import media, recipes


def test_strict_size_rejects_silent_adjustments():
    assert media.parse_size("1024x768", strict=True) == (1024, 768)
    for bad in ("not-a-size", "1000x1000", "2048x1024"):
        with pytest.raises(HTTPException):
            media.parse_size(bad, strict=True)


def test_video_timing_rejects_silent_frame_clamp():
    assert media.normalize_video_timing(10, 24) == (240, 10.0)
    with pytest.raises(HTTPException):
        media.normalize_video_timing(10, 30)


def test_diffusion_params_reject_unbounded_legacy_knobs():
    assert media.diffusion_params("FLUX.2 [klein]", {}) == (4, 1.0, "euler")
    for overrides in ({"steps": 0}, {"steps": media.MAX_STEPS + 1}, {"cfg_scale": media.MAX_CFG_SCALE + 1}):
        with pytest.raises(HTTPException):
            media.diffusion_params("sdxl", overrides)
    with pytest.raises(HTTPException):
        media.diffusion_params("sdxl", {"sampler": "../../bad"})


def test_seed_contract_randomizes_only_when_omitted():
    assert media.normalize_seed(0) == 0
    assert media.normalize_seed("42") == 42
    assert isinstance(media.normalize_seed(None), int)
    assert media.seeds_for_outputs(42, 3) == [42, 43, 44]


def test_recipe_resolver_preserves_explicit_zero_seed():
    recipes.register_recipe("0xseedzero", "seed-zero-test", {
        "_grid": {"vars": {"seed": "3.inputs.seed"}, "modelName": "seed-zero-test"},
        "3": {"inputs": {"seed": 123}},
    })
    out = recipes.resolve("0xseedzero", {"seed": 0})
    assert out["seed"] == 0
    assert out["spec"]["3"]["inputs"]["seed"] == 0
