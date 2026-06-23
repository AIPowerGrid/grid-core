# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for den.py — the work-measurement formula.

This is the on-the-money path. Every paid AIPG flows through this function's
output. Bugs here mean wrong payouts. The contract layer trusts these numbers,
which means we have to.

The formula:
    parameter_bonus = (max(model_mult, 13) / 13) ** 0.20
    den = output_tokens * parameter_bonus * model_mult / 125 * context_mult
    den = max(round(den, 2), 0.1)

Where:
    model_mult comes from exact MODEL_REGISTRY matches; unknown names use the
    conservative default to avoid reward farming by fake "...-405b" names.
    context_mult = clamp(1.2 + 2.2 ** log2(max(prompt_tokens/1024, 0.1)), 0.1, 30)
"""

from __future__ import annotations

import math

import pytest

from grid_api.services.den import (
    DEFAULT_MULTIPLIER,
    MODEL_REGISTRY,
    calculate_context_multiplier,
    calculate_den,
    den_to_units,
    estimate_model_multiplier,
)


# ============ MODEL MULTIPLIER ============


@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("qwen3-27b", 27.0),
        ("Qwen3-27B", 27.0),       # case-insensitive exact registry key
        ("qwen3.6-27b", 27.0),
        ("gpt-oss-120b", 120.0),
        ("gpt-oss-20b", 20.0),
    ],
)
def test_model_multiplier_uses_registry(model_name, expected):
    assert estimate_model_multiplier(model_name) == expected


def test_model_multiplier_unknown_model_uses_default():
    """Falls back to DEFAULT_MULTIPLIER for models without a size in the name."""
    assert estimate_model_multiplier("some-unknown-model") == DEFAULT_MULTIPLIER
    assert estimate_model_multiplier("") == DEFAULT_MULTIPLIER


def test_model_multiplier_does_not_parse_fake_large_names():
    """Fake/unregistered names should not farm den by embedding a large size."""
    assert estimate_model_multiplier("deepseek-distill-1.5b-from-70b") == DEFAULT_MULTIPLIER
    assert estimate_model_multiplier("totally-real-405b") == DEFAULT_MULTIPLIER


# ============ CONTEXT MULTIPLIER ============


def test_context_multiplier_baseline_at_1024_tokens():
    """At prompt_tokens=1024, log2(1) = 0, so context_mult = 1.2 + 2.2**0 = 2.2"""
    mult = calculate_context_multiplier(1024)
    assert mult == pytest.approx(2.2, abs=0.01)


def test_context_multiplier_grows_with_context():
    """Longer contexts cost more. Strict monotonic until the 30x cap."""
    mults = [calculate_context_multiplier(n) for n in (512, 1024, 2048, 4096, 8192, 16384)]
    for i in range(1, len(mults)):
        assert mults[i] > mults[i - 1], f"context mult should grow: {mults}"


def test_context_multiplier_clamped_high():
    """Very large context should hit the 30x cap, not unbounded growth."""
    assert calculate_context_multiplier(10_000_000) == 30.0


def test_context_multiplier_clamped_low():
    """Zero / negative prompt tokens normalize to a small positive base."""
    assert calculate_context_multiplier(0) >= 0.1
    assert calculate_context_multiplier(-5) >= 0.1


def test_context_multiplier_short_prompt_is_below_baseline():
    """Prompts shorter than 1024 should cost less per token than baseline."""
    short = calculate_context_multiplier(128)
    baseline = calculate_context_multiplier(1024)
    assert short < baseline


# ============ DEN CALCULATION — END-TO-END ============


def test_zero_output_tokens_returns_minimum():
    """Worker did some work (answered the call) but generated nothing —
    they still get the floor reward of 0.1 den so honest no-ops don't get
    zero. Prevents an attacker submitting empty completions for free, since
    0.1 is so small it can't be farmed economically."""
    assert calculate_den(0, prompt_tokens=100, model_name="llama-3-8b") == 0.1
    assert calculate_den(-1, prompt_tokens=100, model_name="llama-3-8b") == 0.1


def test_minimum_floor_enforced_on_tiny_outputs():
    """Even one token on a tiny model with no context should be at least 0.1."""
    den = calculate_den(1, prompt_tokens=0, model_name="1b-toy")
    assert den >= 0.1


def test_den_scales_linearly_with_output_tokens():
    """Doubling output tokens (same model, same context) should double den."""
    den_100 = calculate_den(100, prompt_tokens=1024, model_name="llama-3-8b")
    den_200 = calculate_den(200, prompt_tokens=1024, model_name="llama-3-8b")
    # Allow small rounding tolerance from round(..., 2)
    assert den_200 == pytest.approx(den_100 * 2, rel=0.01)


def test_den_scales_with_model_size():
    """A larger model earns more per token than a smaller one
    (same output tokens, same context)."""
    den_default = calculate_den(100, prompt_tokens=1024, model_name="unregistered-3b-tiny")
    den_120b = calculate_den(100, prompt_tokens=1024, model_name="gpt-oss-120b")
    assert den_120b > den_default


def test_den_scales_with_context_length():
    """Longer context costs more even with same output and model."""
    den_short = calculate_den(100, prompt_tokens=512, model_name="llama-3-8b")
    den_long = calculate_den(100, prompt_tokens=8192, model_name="llama-3-8b")
    assert den_long > den_short


def test_baseline_calibration_holds():
    """Headline calibration from the docstring:
       Unknown/unregistered models use DEFAULT_MULTIPLIER=1.0, so the
       anti-gaming baseline is intentionally lower than registered model pay.
    """
    den = calculate_den(100, prompt_tokens=1024, model_name="3b")
    # Document the actual value so a regression is visible
    assert den == pytest.approx(1.76, abs=0.1)


def test_returns_float_rounded_to_2_decimals():
    """Den values should always be 2-decimal floats — settles cleanly into
    integer 'work units' when scaled up for the on-chain commit."""
    den = calculate_den(100, prompt_tokens=1024, model_name="llama-3-8b")
    # round(x, 2) gives back a float that, when * 100, is approximately integer
    assert math.isclose(den * 100, round(den * 100), abs_tol=1e-6)


def test_unknown_model_uses_default_multiplier_in_calc():
    """An unknown model name should not zero out — uses DEFAULT_MULTIPLIER."""
    den = calculate_den(100, prompt_tokens=1024, model_name="some-mystery-model")
    assert den > 0


def test_den_to_units_preserves_fractional_earners():
    assert den_to_units(0.1) == 100_000
    assert den_to_units("1.234567") == 1_234_567


def test_extreme_context_does_not_overflow_or_explode():
    """Pathological 10M context shouldn't return inf or crash."""
    den = calculate_den(100, prompt_tokens=10_000_000, model_name="llama-3-8b")
    assert den > 0
    assert math.isfinite(den)


# ============ ATTACK SHAPES ============


def test_short_output_long_context_does_not_pay_disproportionately():
    """Cheap-to-generate output with crafted huge context shouldn't farm
    disproportionate den. The exponential context_mult caps at 30x.
    """
    den = calculate_den(1, prompt_tokens=100_000, model_name="llama-3-8b")
    # 1 token × (8/13)^0.2 ≈ 0.91 × 8/125 = 0.058 × 30 (max ctx) ≈ 1.75
    # so this should be ~1.75-ish, never explosive
    assert den < 5, f"single-token + huge-context payout looks attackable: {den}"
