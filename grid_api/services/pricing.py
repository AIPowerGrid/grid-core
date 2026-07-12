# ⚠️ WIRED-DARK (2026-06-23): the request path quotes against this book via
# credits.charge_request, but only in dry-run (GRID_CHARGING_ENABLED=0) — the quote
# is logged, never billed. Re-peg prices here before charging goes live. See task #73.

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

The price book is sourced from the cheapest competitor's USD sheet, halved
(`half_of`) — our standing "half of the cheapest competitor" position. Re-peg by
editing the USD numbers here; nothing converts at request time.

    cost_usd = (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000
"""

from dataclasses import dataclass

MICRO = 1_000_000  # micro-USD per USD (the ledger's integer unit)


@dataclass
class ModelPrice:
    input_per_mtok: float   # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens
    image_per_image: float = 0.0   # USD per image
    video_per_second: float = 0.0  # USD per second of video
    mesh_per_generation: float = 0.0  # USD per completed 3D generation


def half_of(usd_input: float, usd_output: float, **media) -> ModelPrice:
    """Cheapest-competitor USD $/Mtok → HALF. The price book stays in USD; no
    conversion happens at request time."""
    return ModelPrice(
        input_per_mtok=usd_input / 2,
        output_per_mtok=usd_output / 2,
        image_per_image=(media["usd_image"] / 2) if media.get("usd_image") else 0.0,
        video_per_second=(media["usd_video_sec"] / 2) if media.get("usd_video_sec") else 0.0,
        mesh_per_generation=(media["usd_mesh"] / 2) if media.get("usd_mesh") else 0.0,
    )


# ── Price book (keyed by lowercased model name) — HALF cheapest competitor ──
# half_of() takes the competitor USD floor and stores HALF of it, in USD.
# KEYS MUST MATCH the model name workers advertise. Sourced 2026-06-15.
PRICING: dict[str, ModelPrice] = {
    "gpt-oss-120b":       half_of(0.15, 0.60),   # floor Fireworks/Groq
    "deepseek-v4-flash":  half_of(0.14, 0.28),   # floor Fireworks
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
    # Pegged 2026-07-05 to competitor floors, HALVED (half_of divides by 2), so the
    # arg is the competitor floor and the stored price is what we charge. Keys are
    # the lowercased model names workers advertise. Effective charge in comments.
    "z-image-turbo":        half_of(0, 0, usd_image=0.006),      # → $0.003/image (turbo/schnell-tier)
    "flux.2 klein 4b fp8":  half_of(0, 0, usd_image=0.02),       # → $0.010/image (Flux distilled 4B)
    "krea 2 turbo":         half_of(0, 0, usd_image=0.03),       # → $0.015/image (quality turbo)
    "ltx-2.3":              half_of(0, 0, usd_video_sec=0.04),   # → $0.020/second video (Justin)
    # Initial guarded peg for a multi-minute textured mesh workload. Re-peg from
    # measured worker cost before enabling charging; explicit is safer than free.
    "trellis2":             ModelPrice(0, 0, mesh_per_generation=0.25),
}

BLOCK_UNPRICED = False  # unpriced model → 0 (free) unless flipped


def register(model: str, price: ModelPrice) -> None:
    if model:
        PRICING[model.lower().strip()] = price


def get_price(model: str) -> ModelPrice | None:
    return PRICING.get((model or "").lower().strip())


def is_priced(model: str) -> bool:
    """True if the model has an entry in the price book. Enforce mode
    default-denies anything unpriced so a renamed/new model can't be free."""
    return get_price(model) is not None


def quote_text(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Cost of a text completion, in integer micro-USD. 0 if unpriced."""
    p = get_price(model)
    if not p:
        return 0
    usd = (prompt_tokens * p.input_per_mtok + completion_tokens * p.output_per_mtok) / 1_000_000.0
    return int(round(usd * MICRO))


def quote_image(model: str, n: int = 1) -> int:
    p = get_price(model)
    return int(round((p.image_per_image * max(n, 1)) * MICRO)) if p else 0


def quote_video(model: str, seconds: float = 0.0) -> int:
    p = get_price(model)
    return int(round((p.video_per_second * max(seconds, 0.0)) * MICRO)) if p else 0


def quote_3d(model: str, n: int = 1) -> int:
    p = get_price(model)
    return int(round((p.mesh_per_generation * max(n, 1)) * MICRO)) if p else 0
