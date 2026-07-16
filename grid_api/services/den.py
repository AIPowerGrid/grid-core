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

# ── Settlement scaling ──
# Den is a float meter (per-job ~0.1–50). On-chain payouts and Merkle leaves
# need integers, and a bare int(den) would round every sub-1.0 earner to ZERO.
# Multiply by DEN_SCALE at the float→int boundary (settlement) so 6 decimals of
# den survive. THE CONTRACT MUST USE THE SAME SCALE. 1e6 = micro-den (USDC-like),
# ample headroom: a 100k-den epoch → 1e11, far inside uint256.
DEN_SCALE = 1_000_000

# ── Model size multipliers — sourced from the on-chain ModelVault registry ──
# A model's den multiplier is its parameter count (in billions). The SOURCE OF
# TRUTH is the ModelVault contract; `model_registry.sync_from_modelvault()`
# populates MODEL_REGISTRY (keyed by lowercased model name). We deliberately do
# NOT parse the multiplier out of the model NAME anymore — a worker could
# advertise "myllama-405b", serve a 1B model, and farm a 405x reward (and the
# old substring match even mis-scored "Qwen3.6-27B" as 7x by matching "7b").
# Unknown / unregistered models get DEFAULT_MULTIPLIER, never a name-derived one.
MODEL_REGISTRY: dict[str, float] = {
    # Seed of currently-approved models; replaced/augmented by the ModelVault sync.
    # Keyed by lowercased advertised model name -> size multiplier (~param billions).
    "qwen3-27b": 27.0,
    "qwen3.6-27b": 27.0,
    "gpt-oss-120b": 120.0,
    "gpt-oss-20b": 20.0,
}

# Conservative BASELINE for models not in the registry. Was 7.0, which let any worker
# advertising an unregistered name farm 7x with no proof it serves that model (audit H1).
# Now 1.0: an unknown model earns baseline; register it (seed above / ModelVault) to earn
# its true larger multiplier. Bounds reward-gaming to ~baseline until identity is proven.
DEFAULT_MULTIPLIER = 1.0


def register_model(model_name: str, param_billions: float) -> None:
    """Add/update a model's multiplier (called by the ModelVault sync)."""
    if model_name:
        MODEL_REGISTRY[model_name.lower().strip()] = float(param_billions)


def estimate_model_multiplier(model_name: str) -> float:
    """Look up a model's size multiplier from the ModelVault-sourced registry.

    Exact (case-insensitive) name match only — no name parsing. Unregistered
    models get DEFAULT_MULTIPLIER so a fake large-model name can't farm den.
    """
    return MODEL_REGISTRY.get((model_name or "").lower().strip(), DEFAULT_MULTIPLIER)


def den_to_units(den: float | int | str) -> int:
    """Convert float den to integer on-chain/Merkle units once.

    The DB ledger stores human-scale float den. Settlement snapshots, Merkle
    leaves, and contract calls use integer micro-den so fractional earners do
    not round to zero. Call this at the ledger->settlement boundary only.
    """
    return int(round(float(den) * DEN_SCALE))


# ── Server-side output token counting (worker-independent, anti-gaming) ──
_TOKEN_ENC = None


def count_tokens(text: str) -> int:
    """Count output tokens SERVER-SIDE so a worker can't inflate its reward.

    Uses tiktoken's o200k_base as a deterministic, model-agnostic proxy (far
    better than word-splitting, which undercounts ~25% and mangles code/CJK).
    Falls back to a ~4-chars/token estimate if tiktoken isn't installed.
    """
    if not text:
        return 0
    global _TOKEN_ENC
    try:
        import tiktoken
        if _TOKEN_ENC is None:
            _TOKEN_ENC = tiktoken.get_encoding("o200k_base")
        return len(_TOKEN_ENC.encode(text, disallowed_special=()))
    except Exception:
        return max(1, len(text) // 4)


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
# Audio reward is duration-based and server-capped; tune after the three V1
# hardware benchmarks. It never trusts worker-reported duration.
DEN_PER_AUDIO_SECOND = 0.06


def calculate_media_den(
    job_type: str,
    width: int,
    height: int,
    steps: int = 20,
    n: int = 1,
    frames: int = 0,
    seconds: float = 0.0,
) -> float:
    """Calculate den for an image or video generation.

    Scales with actual compute: megapixels x steps x outputs (x frames for
    video). Server-side inputs only — all values come from the job payload the
    server constructed, never from worker self-reporting.
    """
    megapixels = max((width * height) / 1_048_576, 0.01)
    steps = max(steps, 1)

    if job_type == "audio":
        den = max(float(seconds), 1.0) * DEN_PER_AUDIO_SECOND * max(n, 1)
    elif job_type == "video":
        den = megapixels * steps * max(frames, 1) * DEN_PER_MEGAPIXEL_FRAME_STEP
    else:
        den = megapixels * steps * max(n, 1) * DEN_PER_MEGAPIXEL_STEP

    den = max(round(den, 2), 0.1)
    logger.debug(
        f"Media den calc: {job_type} {width}x{height} steps={steps} n={n} "
        f"frames={frames} → {den} den"
    )
    return den
