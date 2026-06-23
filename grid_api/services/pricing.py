# ⚠️ UNWIRED / SHIP-DARK (2026-06-22 audit): no live request-path code imports this module.
# It is built but NOT active — do NOT assume billing/slashing/registry-sync runs. Wire it
# intentionally (+ tests) before relying on it. See task #62.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-model charge pricing — AIPG-NATIVE (no runtime oracle).

What USERS pay, denominated in AIPG (distinct from the on-chain `denMultiplier`,
which is what WORKERS earn). Prices are stored as **AIPG per 1,000,000 tokens**;
the ledger charges in integer **micro-AIPG** (AIPG × 1e6 — fits BigInteger,
6 decimals of granularity, far finer than any per-request cost).

The competitor cost sheet is in USD, so `half_of()` converts the USD floor →
half → AIPG **using `AIPG_USD_RATE` ONCE, at peg time** (deploy/re-peg). That
rate is NOT used per request: a deposit credits AIPG 1:1, and a charge debits
AIPG directly. So the payment path has zero oracle dependency — the only place a
USD rate appears is the deliberate, occasional re-peg of the list prices to track
competitors. Re-peg by updating AIPG_USD_RATE (or by editing the AIPG numbers).

    cost_aipg = (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000
"""

import os
from dataclasses import dataclass

MICRO = 1_000_000  # micro-AIPG per AIPG (the ledger's integer unit)

# Peg-time reference ONLY — used by half_of() to translate the USD competitor
# sheet into AIPG list prices. Never read on the request path. Re-peg here.
AIPG_USD_RATE = float(os.getenv("AIPG_USD_RATE", "0.00123"))


@dataclass
class ModelPrice:
    input_per_mtok: float   # AIPG per 1M input tokens
    output_per_mtok: float  # AIPG per 1M output tokens
    image_per_image: float = 0.0   # AIPG per image
    video_per_second: float = 0.0  # AIPG per second of video


def half_of(usd_input: float, usd_output: float, **media) -> ModelPrice:
    """Cheapest-competitor USD $/Mtok → HALF → AIPG, at the current peg.

    Convenience so the USD cost sheet maps 1:1. The resulting AIPG numbers are
    static until you re-peg; nothing converts at request time."""
    r = AIPG_USD_RATE
    return ModelPrice(
        input_per_mtok=(usd_input / 2) / r,
        output_per_mtok=(usd_output / 2) / r,
        image_per_image=((media["usd_image"] / 2) / r) if media.get("usd_image") else 0.0,
        video_per_second=((media["usd_video_sec"] / 2) / r) if media.get("usd_video_sec") else 0.0,
    )


# ── Price book (keyed by lowercased model name) — HALF cheapest competitor ──
# half_of() takes the competitor USD floor and stores the AIPG-native price.
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
}

BLOCK_UNPRICED = False  # unpriced model → 0 (free) unless flipped


def register(model: str, price: ModelPrice) -> None:
    if model:
        PRICING[model.lower().strip()] = price


def get_price(model: str) -> ModelPrice | None:
    return PRICING.get((model or "").lower().strip())


def quote_text(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Cost of a text completion, in integer micro-AIPG. 0 if unpriced."""
    p = get_price(model)
    if not p:
        return 0
    aipg = (prompt_tokens * p.input_per_mtok + completion_tokens * p.output_per_mtok) / 1_000_000.0
    return int(round(aipg * MICRO))


def quote_image(model: str, n: int = 1) -> int:
    p = get_price(model)
    return int(round((p.image_per_image * max(n, 1)) * MICRO)) if p else 0


def quote_video(model: str, seconds: float = 0.0) -> int:
    p = get_price(model)
    return int(round((p.video_per_second * max(seconds, 0.0)) * MICRO)) if p else 0