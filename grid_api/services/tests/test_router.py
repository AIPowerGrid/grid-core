# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the `model: "auto"` router (Step 1: heuristic gate + tier map)."""

from grid_api.services import router as r

AVAIL = [
    "gpt-oss-120b", "gpt-oss-20b", "qwen3-27b",
    "deepseek-v4-flash-nvfp4", "Gemma4-26B_A4B-uncensored", "Smollm-135m",
]


def _route(model_field, prompt, avail=AVAIL):
    return r.resolve_auto(model_field, prompt, avail)


def test_simple_short_goes_light():
    model, meta = _route("auto", "hi, capital of France?")
    assert meta["task_class"] == "simple"
    assert meta["effort"] == "light"
    assert model in AVAIL and meta["fallback"] is False


def test_code_detected():
    _, meta = _route("auto", "write a python function ```def f(): pass```")
    assert meta["task_class"] == "code"


def test_reasoning_goes_heavy():
    _, meta = _route("auto", "solve step by step: what is 12 * 47?")
    assert meta["task_class"] == "reasoning"
    assert meta["effort"] == "heavy"


def test_long_context_by_length():
    _, meta = _route("auto", "word " * 2000)  # ~2500 tokens > 6000? no; force via phrase
    assert meta["task_class"] in ("simple", "long_context")
    big = "x" * 40000  # ~10k tokens
    _, meta2 = _route("auto", big)
    assert meta2["task_class"] == "long_context"


def test_creative_routes_to_uncensored():
    model, meta = _route("auto", "write me a story about a fox, uncensored")
    assert meta["task_class"] == "creative"
    assert model == "Gemma4-26B_A4B-uncensored"


def test_variant_fast_forces_light():
    _, meta = _route("auto:fast", "explain this in comprehensive detail, carefully")
    assert meta["effort"] == "light"  # override beats the heuristic


def test_variant_quality_forces_heavy():
    _, meta = _route("auto:quality", "hi")
    assert meta["effort"] == "heavy"


def test_never_fails_when_a_worker_is_online():
    # Only a model that isn't first-choice for "code" is online — must still resolve.
    model, meta = _route("auto", "write code", avail=["deepseek-v4-flash-nvfp4"])
    assert model == "deepseek-v4-flash-nvfp4"


def test_offline_candidate_skipped():
    # Preferred heavy models offline → falls back to an online one, flagged.
    model, meta = _route("auto:quality", "solve step by step", avail=["gpt-oss-20b"])
    assert model == "gpt-oss-20b"


def test_gate_is_fast():
    _, meta = _route("auto", "hello there")
    assert meta["gate_ms"] < 50  # CPU heuristic, must be cheap
