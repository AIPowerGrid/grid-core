# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Den (priority credit) calculation for streaming text generation.

Ported from the Flask app's kudos formula with simplifications for the
streaming API. The formula rewards workers based on:
  - Number of output tokens generated
  - Model size (bigger models = more den per token)
  - Context length (longer prompts = exponentially more den)

The base unit: generating 100 tokens with a ~3B model at 1024 context = ~10 den.
"""

import math
import logging

logger = logging.getLogger("grid_api.den")

# Known model size multipliers (parameter count in billions)
# These are approximate — the exact multiplier can be refined over time.
MODEL_MULTIPLIERS = {
    # Small models (1-3B)
    "1b": 1.0, "1.5b": 1.5, "2b": 2.0, "3b": 3.0,
    # Medium models (7-13B)
    "7b": 7.0, "8b": 8.0, "9b": 9.0, "13b": 13.0,
    # Large models (30-70B)
    "30b": 30.0, "34b": 34.0, "35b": 35.0,
    "65b": 65.0, "70b": 70.0,
    # XL models (100B+)
    "120b": 120.0, "180b": 180.0, "405b": 405.0,
}

# Default multiplier for unknown models
DEFAULT_MULTIPLIER = 7.0


def estimate_model_multiplier(model_name: str) -> float:
    """Estimate model size multiplier from the model name.

    Looks for patterns like '7b', '70b', '120b' in the model name.
    Falls back to DEFAULT_MULTIPLIER if no size pattern found.
    """
    name_lower = model_name.lower()
    # Try to extract parameter count from model name
    for size_str, multiplier in sorted(MODEL_MULTIPLIERS.items(), key=lambda x: -x[1]):
        if size_str in name_lower:
            return multiplier
    return DEFAULT_MULTIPLIER


def calculate_context_multiplier(prompt_tokens: int) -> float:
    """Calculate context multiplier based on prompt length.

    Exponential scaling: 1024 tokens = base (1.2x), doubles roughly every 2x context.
    Capped at 30x to prevent abuse.
    """
    if prompt_tokens <= 0:
        prompt_tokens = 1
    base = max(prompt_tokens / 1024, 0.1)
    multiplier = 1.2 + (2.2 ** math.log2(base))
    return min(max(multiplier, 0.1), 30.0)


def calculate_den(
    output_tokens: int,
    prompt_tokens: int = 0,
    model_name: str = "",
    generation_time_seconds: float = 0,
) -> float:
    """Calculate den reward for a streaming text generation.

    Args:
        output_tokens: Number of tokens the worker generated.
        prompt_tokens: Approximate prompt/context token count.
        model_name: Model name (used to estimate size multiplier).
        generation_time_seconds: How long the generation took (for logging).

    Returns:
        Den amount to award the worker.
    """
    if output_tokens <= 0:
        return 0.1  # Minimum award for any work done

    model_mult = estimate_model_multiplier(model_name)
    context_mult = calculate_context_multiplier(prompt_tokens)

    # Base formula: tokens * model_bonus * model_mult / 125
    # This gives ~10 den for 100 tokens on a 3B model at 1024 context
    parameter_bonus = (max(model_mult, 13) / 13) ** 0.20
    den = output_tokens * parameter_bonus * model_mult / 125

    # Apply context multiplier
    den = den * context_mult

    # Minimum 0.1 den for any completed job
    den = max(round(den, 2), 0.1)

    logger.debug(
        f"Den calc: {output_tokens} tokens, model={model_name} ({model_mult}x), "
        f"context={prompt_tokens} ({context_mult:.2f}x) → {den} den"
    )

    return den


# ── Media (image/video) den ──
# Tunable knobs — these are POLICY, expected to be adjusted as real worker
# economics emerge. Kept dead simple on purpose: den is a meter, AIPG/den is
# set by the epoch budget at settlement, so absolute scale here only affects
# relative weighting between job types.

# 1 megapixel-step of diffusion work ≈ this many den.
DEN_PER_MEGAPIXEL_STEP = 0.04
# Video work is costlier per frame than a lone image at the same resolution
# (temporal models, VAE decode over frames) — weight per frame-step.
DEN_PER_MEGAPIXEL_FRAME_STEP = 0.002


def calculate_media_den(
    job_type: str,
    width: int,
    height: int,
    steps: int = 20,
    n: int = 1,
    frames: int = 0,
) -> float:
    """Calculate den for an image or video generation.

    Scales with actual compute: megapixels x steps x outputs (x frames for
    video). Server-side inputs only — all values come from the job payload the
    server constructed, never from worker self-reporting.
    """
    megapixels = max((width * height) / 1_048_576, 0.01)
    steps = max(steps, 1)

    if job_type == "video":
        den = megapixels * steps * max(frames, 1) * DEN_PER_MEGAPIXEL_FRAME_STEP
    else:
        den = megapixels * steps * max(n, 1) * DEN_PER_MEGAPIXEL_STEP

    den = max(round(den, 2), 0.1)
    logger.debug(
        f"Media den calc: {job_type} {width}x{height} steps={steps} n={n} "
        f"frames={frames} → {den} den"
    )
    return den
