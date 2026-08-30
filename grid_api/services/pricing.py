# WIRED-DARK: the request path always quotes against this book, but
# GRID_CHARGING_MODE=off only logs the quote. Re-peg and review prices before
# expanding beyond an allowlisted canary.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-model charge pricing — USD-NATIVE (no oracle on the request path).

What USERS pay, denominated in USD. Prices are stored as **USD per 1,000,000
tokens**; the ledger charges in integer **micro-USD** (USD × 1e6 — fits
BigInteger, 6 decimals of granularity, far finer than any per-request cost).

Why USD, not AIPG: USDC is the unit everyone on Base actually holds and what
x402 settles in, so credits are denominated in USD and a USDC deposit credits
1:1 with zero oracle. AIPG is the worker-stake / reward-share asset (supply
side), not the customer unit of account — see services/economics.py.

Some rates are derived from a reviewed hosted-provider floor through `half_of`;
others are guarded launch pegs based on measured worker cost. Re-peg by editing
the USD numbers here; nothing converts at request time. Public market claims
must come only from the expiring same-model evidence below, never from the
presence of a configured price.

    cost_usd = (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

MICRO = 1_000_000  # micro-USD per USD (the ledger's integer unit)
PRICE_BOOK_VERSION = "2026-08-29-a"
COMPARISON_AS_OF = "2026-08-29T00:00:00Z"
COMPARISON_VALID_UNTIL = "2026-09-29T00:00:00Z"


@dataclass
class ModelPrice:
    input_per_mtok: float   # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens
    image_per_image: float = 0.0   # USD per image
    video_per_second: float = 0.0  # USD per second of video
    audio_per_second: float = 0.0  # USD per second of generated audio
    mesh_per_generation: float = 0.0  # USD per completed 3D generation


def half_of(usd_input: float, usd_output: float, **media) -> ModelPrice:
    """Cheapest-competitor USD $/Mtok → HALF. The price book stays in USD; no
    conversion happens at request time."""
    return ModelPrice(
        input_per_mtok=usd_input / 2,
        output_per_mtok=usd_output / 2,
        image_per_image=(media["usd_image"] / 2) if media.get("usd_image") else 0.0,
        video_per_second=(media["usd_video_sec"] / 2) if media.get("usd_video_sec") else 0.0,
        audio_per_second=(media["usd_audio_sec"] / 2) if media.get("usd_audio_sec") else 0.0,
        mesh_per_generation=(media["usd_mesh"] / 2) if media.get("usd_mesh") else 0.0,
    )


# ── Price book (keyed by lowercased model name) ──
# For competitor-derived entries, half_of() stores half the reviewed USD floor.
# KEYS MUST MATCH the model name workers advertise. Last media peg: 2026-07-28.
PRICING: dict[str, ModelPrice] = {
    "gpt-oss-120b":       half_of(0.15, 0.60),   # floor Fireworks/Groq
    "deepseek-v4-flash":  half_of(0.14, 0.28),   # floor Fireworks
    # Guarded launch pegs for Grid-hosted models without a stable public API
    # comparator. Re-peg from measured worker cost before the global live flip.
    "qwen3-27b":          ModelPrice(0.05, 0.15),
    "smollm-135m":        ModelPrice(0.005, 0.01),
    "deepseek-v4-pro":    half_of(0.40, 1.20),
    "minimax-2.5-fast":   half_of(0.60, 2.40),
    "minimax-2.7-fast":   half_of(0.60, 2.40),
    "kimi-k2":            half_of(0.95, 4.00),
    "glm-5.1":            half_of(1.00, 3.20),   # floor ZAI
    "glm-5-turbo":        half_of(1.20, 4.00),
    "glm-4.7":            half_of(2.25, 2.75),
    "mimo-v2.5":          half_of(0.14, 0.28),
    "mimo-v2.5-pro":      half_of(0.435, 0.87),

    # ── Media (image per-image, video per-second) ──
    # Pegged 2026-07-28 to competitor floors, HALVED (half_of divides by 2), so the
    # arg is the competitor floor and the stored price is what we charge. Keys are
    # the lowercased model names workers advertise. Effective charge in comments.
    "z-image-turbo":        half_of(0, 0, usd_image=0.006),      # → $0.003/image (1 MP turbo)
    "flux.2 klein 4b fp8":  half_of(0, 0, usd_image=0.02),       # → $0.010/image (Flux distilled 4B)
    "krea 2 turbo":         half_of(0, 0, usd_image=0.01),       # → $0.005/image (txt2img or img2img)
    "ltx-2.3":              half_of(0, 0, usd_video_sec=0.04),   # → $0.020/second video (Justin)
    # Aggressive 3090-native launch peg. Production telemetry through 2026-07-26
    # measured 151 jobs averaging 78.8 output seconds in 10.3 runtime seconds.
    # At $0.0002/output-second, a one-minute song costs $0.012 while measured
    # full-utilization gross remains about $5.50/hour before network costs.
    "ace-step-v1.5-xl-turbo": ModelPrice(0, 0, audio_per_second=0.0002),
    # Initial guarded peg for a multi-minute textured mesh workload. Re-peg from
    # measured worker cost before enabling charging; explicit is safer than free.
    "trellis2":             ModelPrice(0, 0, mesh_per_generation=0.25),
}

# Public recipe names and worker variants share the canonical model's price.
# Aliases are explicit so an arbitrary renamed worker model still fails closed.
PRICE_ALIASES: dict[str, str] = {
    "deepseek-v4-flash-nvfp4": "deepseek-v4-flash",
    "ltx director 2.0": "ltx-2.3",
    "ltx-2.3 audio": "ltx-2.3",
}

# These are narrowly comparable public benchmarks, not a claim that every Grid
# model is cheaper than every hosted alternative. Each source is the named
# provider's own current price page. The comparison disappears after the review
# window instead of letting a stale marketing claim live forever.
PUBLIC_COMPARISON_BENCHMARKS: tuple[dict, ...] = (
    {
        "id": "gpt-oss-120b-standard-token-rates",
        "model": "gpt-oss-120b",
        "modality": "text",
        "provider": "Groq",
        "source_url": "https://console.groq.com/docs/model/openai/gpt-oss-120b",
        "basis": "same model; 1M uncached input tokens plus 1M output tokens",
        "workload": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "competitor_micro_usd": 750_000,
        "competitor_rates": {
            "input_per_mtok_usd": 0.15,
            "output_per_mtok_usd": 0.60,
        },
    },
    {
        "id": "z-image-turbo-one-megapixel",
        "model": "z-image-turbo",
        "modality": "image",
        "provider": "fal",
        "source_url": "https://fal.ai/models/fal-ai/z-image/turbo",
        "basis": "same model; one 1-megapixel image without optional prompt expansion",
        "workload": {"images": 1, "megapixels": 1},
        "competitor_micro_usd": 5_000,
        "competitor_rates": {"per_megapixel_usd": 0.005},
    },
)

def register(model: str, price: ModelPrice) -> None:
    if model:
        PRICING[model.lower().strip()] = price


def get_price(model: str) -> ModelPrice | None:
    key = (model or "").lower().strip()
    return PRICING.get(PRICE_ALIASES.get(key, key))


def is_priced(model: str) -> bool:
    """Compatibility check for callers that do not yet know the modality."""
    return get_price(model) is not None


def is_priced_for(model: str, job_type: str) -> bool:
    """Return whether ``model`` has a positive rate for ``job_type``.

    An entry for a different modality is not a price. This is the fail-closed
    boundary that prevents, for example, an LTX video rate from making an image
    request look intentionally free.
    """
    p = get_price(model)
    if not p:
        return False
    rates = {
        "text": p.input_per_mtok > 0 or p.output_per_mtok > 0,
        "image": p.image_per_image > 0,
        "video": p.video_per_second > 0,
        "audio": p.audio_per_second > 0,
        "3d": p.mesh_per_generation > 0,
    }
    return bool(rates.get((job_type or "").lower()))


def _micro_usd(usd: float) -> int:
    """Round a positive quote up to the smallest ledger unit.

    Normal rounding made tiny but explicitly priced requests free. Ceiling keeps
    the price book's default-deny promise while adding at most one micro-dollar.
    """
    return math.ceil(usd * MICRO) if usd > 0 else 0


def quote_text(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Cost of a text completion, in integer micro-USD. 0 if unpriced."""
    p = get_price(model)
    if not p:
        return 0
    usd = (prompt_tokens * p.input_per_mtok + completion_tokens * p.output_per_mtok) / 1_000_000.0
    return _micro_usd(usd)


def quote_image(model: str, n: int = 1) -> int:
    p = get_price(model)
    return _micro_usd(p.image_per_image * max(n, 1)) if p else 0


def quote_video(model: str, seconds: float = 0.0) -> int:
    p = get_price(model)
    return _micro_usd(p.video_per_second * max(seconds, 0.0)) if p else 0


def quote_audio(model: str, seconds: float = 0.0) -> int:
    p = get_price(model)
    return _micro_usd(p.audio_per_second * max(seconds, 0.0)) if p else 0


def quote_3d(model: str, n: int = 1) -> int:
    p = get_price(model)
    return _micro_usd(p.mesh_per_generation * max(n, 1)) if p else 0


def _usd(micro_usd: int) -> float:
    return round(micro_usd / MICRO, 6)


def _public_model_price(model: str, price: ModelPrice) -> dict:
    return {
        "model": model,
        "rates": {
            "input_per_mtok_usd": price.input_per_mtok or None,
            "output_per_mtok_usd": price.output_per_mtok or None,
            "per_image_usd": price.image_per_image or None,
            "per_video_second_usd": price.video_per_second or None,
            "per_audio_second_usd": price.audio_per_second or None,
            "per_3d_generation_usd": price.mesh_per_generation or None,
        },
    }


def _comparison_item(benchmark: dict) -> dict:
    workload = benchmark["workload"]
    if benchmark["modality"] == "text":
        aipg_micro = quote_text(
            benchmark["model"],
            workload["input_tokens"],
            workload["output_tokens"],
        )
    elif benchmark["modality"] == "image":
        aipg_micro = quote_image(benchmark["model"], workload["images"])
    else:  # The static benchmark set is intentionally narrow.
        raise ValueError("unsupported public comparison modality")

    competitor_micro = int(benchmark["competitor_micro_usd"])
    if aipg_micro <= 0 or competitor_micro <= 0:
        raise ValueError("public comparison references an unpriced workload")
    savings_bps = (competitor_micro - aipg_micro) * 10_000 // competitor_micro
    return {
        **{key: value for key, value in benchmark.items() if key != "competitor_micro_usd"},
        "aipg_usd": _usd(aipg_micro),
        "competitor_usd": _usd(competitor_micro),
        "savings_percent": round(savings_bps / 100, 2),
    }


def public_catalog(now: datetime | None = None) -> dict:
    """Return a read-only, versioned public price book and fresh comparisons.

    Comparison evidence expires independently from the charge book. Stale
    evidence is omitted, while the exact configured AIPG rates remain visible.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("pricing catalog time must be timezone-aware")
    as_of = datetime.fromisoformat(COMPARISON_AS_OF.replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(COMPARISON_VALID_UNTIL.replace("Z", "+00:00"))
    if current < as_of:
        comparison_status = "not_yet_valid"
    elif current <= valid_until:
        comparison_status = "current"
    else:
        comparison_status = "stale"
    comparisons = (
        [_comparison_item(item) for item in PUBLIC_COMPARISON_BENCHMARKS]
        if comparison_status == "current"
        else []
    )
    return {
        "schema": "aipg.pricing.v1",
        "currency": "USD",
        "ledger_unit": "micro_usd",
        "price_book": {
            "version": PRICE_BOOK_VERSION,
            "availability": (
                "Configured rates do not assert that a model is online; use /v1/status/models."
            ),
            "models": [
                _public_model_price(model, PRICING[model]) for model in sorted(PRICING)
            ],
            "aliases": dict(sorted(PRICE_ALIASES.items())),
        },
        "comparison_evidence": {
            "status": comparison_status,
            "as_of": COMPARISON_AS_OF,
            "valid_until": COMPARISON_VALID_UNTIL,
            "items": comparisons,
            "scope": (
                "Only the named, same-model reference workloads are compared. "
                "Availability, latency, reliability, and output quality are not price claims."
            ),
        },
    }
