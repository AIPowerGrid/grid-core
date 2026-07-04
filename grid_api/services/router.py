# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`model: "auto"` router — Step 1 (heuristic gate + curated tier map).

Resolves the virtual model `auto` (and `auto:fast` / `auto:quality`) to a
concrete online model:

    classify(prompt) -> task_class        # simple | code | reasoning | long_context | creative
    effort(prompt)   -> light | heavy     # RouteLLM-style strong/weak axis
    tier_map[class][effort] -> ranked candidate models
    -> first candidate that is actually online (best-model pick)

This is deliberately CPU-cheap and dependency-free (rules only). The gate is a
single function so a smarter classifier (embedding / ModernBERT) can drop in
behind `classify()` later without touching callers. Worker-level scoring (pick
the fastest/most-reliable replica via validator evidence) is Step 2 — this step
picks the MODEL and leaves worker selection to the normal dispatch path.

The tier map is curated behind the scenes: edit `routing.json` (or the file at
$GRID_ROUTING_CONFIG). Users only ever see `auto`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger("grid_api.router")

AUTO_MODELS = {"auto", "auto:fast", "auto:quality"}

# Curated default tier map: task_class -> effort -> ranked model candidates.
# Overridable via a JSON file at $GRID_ROUTING_CONFIG (same shape). Unknown /
# offline models are skipped at pick time, so it's safe to list models that
# aren't always online.
_DEFAULT_CONFIG: dict[str, Any] = {
    "classes": {
        "simple":       {"light": ["gpt-oss-20b", "deepseek-v4-flash-nvfp4"], "heavy": ["gpt-oss-120b"]},
        "code":         {"light": ["gpt-oss-20b"],                            "heavy": ["qwen3-27b", "gpt-oss-120b"]},
        "reasoning":    {"light": ["qwen3-27b"],                              "heavy": ["gpt-oss-120b", "qwen3-27b"]},
        "long_context": {"light": ["qwen3-27b"],                              "heavy": ["qwen3-27b", "gpt-oss-120b"]},
        "creative":     {"light": ["Gemma4-26B_A4B-uncensored"],             "heavy": ["Gemma4-26B_A4B-uncensored", "gpt-oss-120b"]},
    },
    "default": {"light": ["gpt-oss-20b", "deepseek-v4-flash-nvfp4"], "heavy": ["gpt-oss-120b", "qwen3-27b"]},
}


def _load_config() -> dict[str, Any]:
    path = os.getenv("GRID_ROUTING_CONFIG", "").strip()
    if path:
        try:
            with open(path) as f:
                cfg = json.load(f)
            cfg.setdefault("classes", _DEFAULT_CONFIG["classes"])
            cfg.setdefault("default", _DEFAULT_CONFIG["default"])
            return cfg
        except Exception as e:  # never let a bad file break routing
            logger.warning(f"routing config {path} unreadable ({e}); using defaults")
    return _DEFAULT_CONFIG


# ── the gate ───────────────────────────────────────────────────────────────
_CODE_RE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b|\bregex\b|\bSQL\b|\bstack ?trace\b|\bcompile|\bbug\b|\btraceback\b", re.I)
_REASON_RE = re.compile(r"\bstep[- ]by[- ]step\b|\bprove\b|\bcalculate\b|\bsolve\b|\bhow many\b|\breason\b|\bthink (?:hard|carefully|step)\b|\d+\s*[+\-*/x]\s*\d+", re.I)
_CREATIVE_RE = re.compile(r"\bwrite (?:me )?a (?:story|poem|song|script)\b|\brole ?play\b|\bpoem\b|\bfiction\b|\buncensored\b|\bnsfw\b", re.I)
_LONGCTX_RE = re.compile(r"\bthe (?:following|document|text|article|transcript) (?:above|below)?\b|\bsummari[sz]e (?:the|this)\b|\bbased on the (?:text|document|above)\b", re.I)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # ~4 chars/token, good enough for a gate


def classify(prompt: str, tokens: int) -> str:
    """task_class from cheap surface features. Order = priority."""
    if tokens > 6000 or _LONGCTX_RE.search(prompt):
        return "long_context"
    if _CODE_RE.search(prompt):
        return "code"
    if _CREATIVE_RE.search(prompt):
        return "creative"
    if _REASON_RE.search(prompt):
        return "reasoning"
    return "simple"


def effort(prompt: str, tokens: int, task_class: str, override: str | None = None) -> str:
    """light|heavy — the strong/weak axis. `override` from auto:fast|auto:quality."""
    if override in ("light", "heavy"):
        return override
    if task_class in ("reasoning", "long_context"):
        return "heavy"
    if tokens > 1500:
        return "heavy"
    if re.search(r"\b(carefully|in detail|comprehensive|architect|design|complex|thorough)\b", prompt, re.I):
        return "heavy"
    return "light"


def _pin(cfg: dict[str, Any], task_class: str) -> str | None:
    """Operator override: prefer/force a specific model.

    Precedence: env GRID_ROUTING_PIN (global, instant, no redeploy) > config
    "pin". Config "pin" may be a string (global) or a {task_class: model} map.
    The pin is honored only if that model is actually online (else we fall
    through to normal routing, so a pin can never take `auto` offline).
    """
    env = os.getenv("GRID_ROUTING_PIN", "").strip()
    if env:
        return env
    pin = cfg.get("pin")
    if isinstance(pin, str) and pin:
        return pin
    if isinstance(pin, dict):
        return pin.get(task_class) or pin.get("*")
    return None


def _variant_override(model_field: str) -> str | None:
    if model_field == "auto:fast":
        return "light"
    if model_field == "auto:quality":
        return "heavy"
    return None


def resolve_auto(model_field: str, prompt: str, available: list[str]) -> tuple[str, dict[str, Any]]:
    """Resolve `auto*` to a concrete ONLINE model + routing metadata.

    Never fails when any text worker is online: candidates that aren't online are
    skipped; if none of a class's candidates are online we fall back through the
    other effort tier, the default tier, then any available model.
    """
    t0 = time.time()
    cfg = _load_config()
    tokens = _approx_tokens(prompt)
    task_class = classify(prompt, tokens)
    eff = effort(prompt, tokens, task_class, override=_variant_override(model_field))

    avail = set(available)

    # Operator override: a pinned model wins outright when it's online. Keeps the
    # classified task_class/effort in the metadata for observability.
    pin = _pin(cfg, task_class)
    if pin and pin in avail:
        meta = {
            "auto": True, "requested": model_field, "resolved_model": pin,
            "task_class": task_class, "effort": eff,
            "gate_ms": round((time.time() - t0) * 1000, 2),
            "fallback": False, "pinned": True,
        }
        logger.info(f"auto route: {model_field} -> {pin} (PINNED, class={task_class})")
        return pin, meta

    class_map = cfg["classes"].get(task_class, cfg["default"])
    other = "light" if eff == "heavy" else "heavy"
    # Preference order: chosen tier → other tier → default(chosen) → default(other).
    ordered: list[str] = (
        class_map.get(eff, [])
        + class_map.get(other, [])
        + cfg["default"].get(eff, [])
        + cfg["default"].get(other, [])
    )
    chosen = next((m for m in ordered if m in avail), None)
    fallback = chosen is None
    if chosen is None:
        chosen = available[0] if available else model_field  # last resort

    meta = {
        "auto": True,
        "requested": model_field,
        "resolved_model": chosen,
        "task_class": task_class,
        "effort": eff,
        "gate_ms": round((time.time() - t0) * 1000, 2),
        "fallback": fallback,
    }
    logger.info(
        f"auto route: {model_field} -> {chosen} (class={task_class} effort={eff} "
        f"fallback={fallback} {meta['gate_ms']}ms)"
    )
    return chosen, meta
