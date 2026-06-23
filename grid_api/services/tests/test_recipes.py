# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the recipe resolver — safe substitution, clamping, seed, governance,
engine-neutrality. Pure stdlib; runnable with `python3 test_recipes.py`."""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from grid_api.services import recipes  # noqa: E402


def _clear():
    recipes._BY_ROOT.clear(); recipes._BY_ID.clear(); recipes._BY_NAME.clear()


def _seed_ltx():
    """ComfyUI engine: nested graph, slots like 'node.inputs.field'."""
    _clear()
    workflow = {
        "_grid": {
            "engine": "comfyui",
            "vars": {"prompt": "6.inputs.text", "seed": "3.inputs.seed",
                     "image": "10.inputs.image", "steps": "3.inputs.steps"},
            "clamps": {"steps": [1, 50]},
            "deterministic": False, "requiredModels": ["LTX-2.3"], "jobType": "video",
        },
        "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20, "cfg": 3.5}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "PLACEHOLDER"}},
        "10": {"class_type": "LoadImage", "inputs": {"image": ""}},
    }
    return recipes.register_recipe("0xabc123", "LTX-2.3 i2v", workflow, recipe_id=1)


def _seed_drawthings():
    """Draw Things engine (Mac): FLAT spec, slots are top-level keys."""
    _clear()
    workflow = {
        "_grid": {
            "engine": "drawthings",
            "vars": {"prompt": "prompt", "seed": "seed", "steps": "steps"},
            "clamps": {"steps": [1, 30]},
            "deterministic": True, "requiredModels": ["sdxl"], "jobType": "image",
        },
        "prompt": "", "seed": 0, "steps": 20, "model": "sdxl_base",
    }
    return recipes.register_recipe("0xdt001", "SDXL (Draw Things)", workflow, recipe_id=2)


def test_basic_substitution():
    _seed_ltx()
    out = recipes.resolve("0xabc123", {"prompt": "a cat", "image": "upload_42", "seed": 7})
    s = out["spec"]
    assert out["engine"] == "comfyui"
    assert s["6"]["inputs"]["text"] == "a cat"
    assert s["10"]["inputs"]["image"] == "upload_42"
    assert s["3"]["inputs"]["seed"] == 7
    assert "_grid" not in s and out["job_type"] == "video"
    print("ok: comfyui nested substitution")


def test_drawthings_flat_engine():
    _seed_drawthings()
    out = recipes.resolve("0xdt001", {"prompt": "a dog", "steps": 25, "seed": 5})
    s = out["spec"]
    assert out["engine"] == "drawthings" and out["deterministic"] is True
    assert s["prompt"] == "a dog"
    assert s["seed"] == 5
    assert s["steps"] == 25                      # in-range, passes the gate unchanged
    assert s["model"] == "sdxl_base"             # untouched
    print("ok: drawthings FLAT-spec substitution (engine-neutral)")


def test_injection_safe():
    _seed_ltx()
    nasty = '","class_type":"Evil"},"99":{"inputs":{},"x":"'
    out = recipes.resolve("0xabc123", {"prompt": nasty})
    s = out["spec"]
    assert s["6"]["inputs"]["text"] == nasty
    assert "99" not in s and len(s) == 3
    assert json.loads(json.dumps(s))["6"]["inputs"]["text"] == nasty
    print("ok: injection-safe")


def test_reject_out_of_range():
    _seed_ltx()
    import pytest
    from grid_api.services.recipes import RecipeError
    # Out-of-range knobs are REJECTED (not silently clamped) — range-GATE, see recipes._gate.
    with pytest.raises(RecipeError):
        recipes.resolve("0xabc123", {"steps": 9999})
    with pytest.raises(RecipeError):
        recipes.resolve("0xabc123", {"steps": -5})
    print("ok: reject out-of-range")


def test_seed_default_and_echo():
    _seed_ltx()
    out = recipes.resolve("0xabc123", {"prompt": "x"})
    assert isinstance(out["seed"], int) and out["seed"] > 0
    assert out["spec"]["3"]["inputs"]["seed"] == out["seed"]
    print("ok: seed default + echo")


def test_unapproved_rejected():
    _seed_ltx()
    try:
        recipes.resolve("0xdeadbeef", {"prompt": "x"}); assert False
    except recipes.RecipeError:
        print("ok: unapproved recipe rejected")


def test_resolve_for_model_by_name_and_fallback():
    _seed_ltx()
    spec = recipes.resolve_for_model("LTX-2.3 i2v", {"prompt": "x"})
    assert spec and spec["recipe_root"] == "0xabc123" and spec["engine"] == "comfyui"
    assert recipes.resolve_for_model("unmapped-model", {"prompt": "x"}) is None
    print("ok: resolve_for_model by name + legacy fallback")


def test_bad_slot_path_rejected():
    _clear()
    recipes.register_recipe("0xbad", "bad", {"_grid": {"vars": {"prompt": "nope.inputs.text"}},
                                             "6": {"inputs": {"text": ""}}}, recipe_id=9)
    try:
        recipes.resolve("0xbad", {"prompt": "x"}); assert False
    except recipes.RecipeError:
        print("ok: bad slot path rejected (can't invent structure)")


def test_dual_seed_list_path():
    _clear()
    recipes.register_recipe("0xseed", "dual", {
        "_grid": {"vars": {"seed": ["a.inputs.noise_seed", "b.inputs.noise_seed"]}},
        "a": {"inputs": {"noise_seed": 0}}, "b": {"inputs": {"noise_seed": 0}},
    }, recipe_id=7)
    s = recipes.resolve("0xseed", {"seed": 123})["spec"]
    assert s["a"]["inputs"]["noise_seed"] == 123 and s["b"]["inputs"]["noise_seed"] == 123
    print("ok: one seed -> multiple slots (list var path)")


def test_import_traces_positive_negative():
    from grid_api.services import recipe_import
    # minimal: a conditioning node splitting positive<-2 negative<-1, two CLIPTextEncode
    wf = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "3": {"class_type": "SomeConditioning",
              "inputs": {"positive": ["2", 0], "negative": ["1", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "5": {"class_type": "LoadImage", "inputs": {"image": ""}},
    }
    recipe, notes = recipe_import.build_recipe(wf, "t", job_type="video")
    v = recipe["_grid"]["vars"]
    assert v["prompt"] == "2.inputs.text"          # traced positive
    assert v["negative_prompt"] == "1.inputs.text"  # traced negative
    assert v["seed"] == "4.inputs.seed" and v["image"] == "5.inputs.image"
    assert recipe["_grid"]["jobType"] == "video" and not notes
    print("ok: import traces positive/negative + detects seed/image")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL RECIPE TESTS PASSED")
