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


def test_env_pin_forces_model(monkeypatch):
    monkeypatch.setenv("GRID_ROUTING_PIN", "qwen3-27b")
    model, meta = _route("auto", "hi there")  # would normally be gpt-oss-20b
    assert model == "qwen3-27b"
    assert meta["pinned"] is True


def test_pin_ignored_when_offline(monkeypatch):
    monkeypatch.setenv("GRID_ROUTING_PIN", "some-model-not-online")
    model, meta = _route("auto", "hi there")
    assert model in AVAIL
    assert meta.get("pinned") is not True  # fell through to normal routing


def test_scores_override_curated_order():
    # 'code' light tier curates gpt-oss-20b first, but if deepseek scores higher
    # among online candidates it should win. (Put both in the default light tier.)
    scores = {"gpt-oss-20b": {"score": 0.2}, "deepseek-v4-flash-nvfp4": {"score": 0.9}}
    model, meta = r.resolve_auto("auto", "hi there", AVAIL, scores=scores)
    assert meta["scored"] is True
    assert model == "deepseek-v4-flash-nvfp4"  # higher score beats curated order
    assert meta.get("score") == 0.9


def test_scores_tie_breaks_to_curated_order():
    scores = {"gpt-oss-20b": {"score": 0.5}, "deepseek-v4-flash-nvfp4": {"score": 0.5}}
    model, _ = r.resolve_auto("auto", "hi there", AVAIL, scores=scores)
    assert model == "gpt-oss-20b"  # curated first wins the tie


def test_no_scores_uses_curated_order():
    model, meta = r.resolve_auto("auto", "hi there", AVAIL, scores=None)
    assert model == "gpt-oss-20b"
    assert meta["scored"] is False


def test_pick_worker_single_is_noop():
    workers = [{"worker_id": "w1", "name": "alpha", "models": ["gpt-oss-20b"], "online": True}]
    name, meta = r.pick_worker("gpt-oss-20b", workers)
    assert name == ""  # 1 candidate → don't constrain
    assert meta["candidates"] == 1


def test_pick_worker_chooses_higher_score(monkeypatch):
    workers = [
        {"worker_id": "w1", "name": "alpha", "models": ["gpt-oss-20b"], "online": True},
        {"worker_id": "w2", "name": "bravo", "models": ["gpt-oss-20b"], "online": True},
    ]
    monkeypatch.setitem(r._SCORE_CACHE, "workers", {
        "gpt-oss-20b|w1": {"score": 0.2},
        "gpt-oss-20b|w2": {"score": 0.9},
    })
    name, meta = r.pick_worker("gpt-oss-20b", workers)
    assert name == "bravo"
    assert meta["worker"] == "bravo" and meta["candidates"] == 2


def test_pick_worker_skips_wrong_model():
    workers = [
        {"worker_id": "w1", "name": "alpha", "models": ["qwen3-27b"], "online": True},
        {"worker_id": "w2", "name": "bravo", "models": ["qwen3-27b"], "online": True},
    ]
    name, _ = r.pick_worker("gpt-oss-20b", workers)  # none serve gpt-oss-20b
    assert name == ""


def test_gate_is_fast():
    _, meta = _route("auto", "hello there")
    assert meta["gate_ms"] < 50  # CPU heuristic, must be cheap
