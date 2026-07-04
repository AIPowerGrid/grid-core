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
    # Step-2 scoring weights (model-level). Tunable via routing.json.
    "weights": {"quality": 1.0, "throughput": 0.4, "latency": 0.5, "failure": 1.0},
}

# Score cache (model-level): avoid a DB hit on every request. Refreshed lazily
# with a TTL; if the refresh ever fails, we keep serving the last good scores
# (or none → curated order), so scoring can never break routing.
_SCORE_CACHE: dict[str, Any] = {"ts": 0.0, "scores": {}, "workers": {}}
_SCORE_TTL = float(os.getenv("GRID_ROUTING_SCORE_TTL", "15") or 15)
_SCORE_WINDOW_H = int(os.getenv("GRID_ROUTING_SCORE_WINDOW_H", "24") or 24)


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


async def _refresh_scores() -> None:
    """Recompute model-level scores from validator quality + live speed.

    Combines: validator avg_score + failed_rate (grid_validator_attestations)
    and decode throughput / latency (ledger via stats._model_stats). Normalized
    across the models present. Wrapped by get_model_scores() which guards it.
    """
    import sqlalchemy as sa
    from datetime import datetime, timedelta, timezone

    from ..database import new_session
    from ..v2.schema import validator_attestations as att
    from ..routers.stats import _perf_by_model

    from ..v2.schema import ledger as led

    cfg = _load_config()
    since = datetime.now(timezone.utc) - timedelta(hours=_SCORE_WINDOW_H)
    quality: dict[str, dict[str, float]] = {}
    speed: dict[str, dict[str, float | None]] = {}
    wq: dict[tuple[str, str], dict[str, float]] = {}   # (model, worker) -> quality
    wp: dict[tuple[str, str], dict[str, float]] = {}   # (model, worker) -> perf
    async with await new_session() as s:
        rows = (
            await s.execute(
                sa.select(
                    att.c.model,
                    sa.func.count().label("n"),
                    sa.func.sum(sa.case((att.c.verdict == "failed", 1), else_=0)).label("failed"),
                    sa.func.avg(att.c.score).label("avg_score"),
                )
                .where(att.c.created >= since, att.c.model.isnot(None))
                .group_by(att.c.model)
            )
        ).mappings().all()
        for r in rows:
            n = int(r["n"]) or 1
            quality[r["model"]] = {
                "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
                "failed_rate": float(r["failed"] or 0) / n,
            }
        for (model, jt), st in (await _perf_by_model(s, since)).items():
            if jt == "text":
                speed[model] = {"tps": st.get("tokens_per_s"), "latency": st.get("avg_latency_s")}

        # Per-WORKER quality (validator attestations, worker_id is String)
        for r in (await s.execute(
            sa.select(
                att.c.worker_id, att.c.model,
                sa.func.count().label("n"),
                sa.func.sum(sa.case((att.c.verdict == "failed", 1), else_=0)).label("failed"),
                sa.func.avg(att.c.score).label("avg_score"),
            ).where(att.c.created >= since, att.c.worker_id.isnot(None), att.c.model.isnot(None))
             .group_by(att.c.worker_id, att.c.model)
        )).mappings().all():
            n = int(r["n"]) or 1
            wq[(r["model"], str(r["worker_id"]))] = {
                "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
                "failed_rate": float(r["failed"] or 0) / n,
            }
        # Per-WORKER perf (ledger, worker_id is Uuid → compare as str)
        for r in (await s.execute(
            sa.select(
                led.c.worker_id, led.c.model,
                sa.func.avg(led.c.duration).label("avg_dur"),
                sa.func.sum(led.c.duration - sa.func.coalesce(led.c.ttft, 0.0)).label("sum_decode"),
                sa.func.sum(led.c.output_units).label("sum_units"),
            ).where(led.c.created >= since, led.c.worker_id.isnot(None),
                    led.c.duration.isnot(None), led.c.duration > 0, led.c.job_type == "text")
             .group_by(led.c.worker_id, led.c.model)
        )).mappings().all():
            sd = float(r["sum_decode"] or 0.0)
            wp[(r["model"], str(r["worker_id"]))] = {
                "tps": round(int(r["sum_units"] or 0) / sd, 1) if sd > 0 else None,
                "latency": round(float(r["avg_dur"]), 2) if r["avg_dur"] is not None else None,
            }

    models = set(quality) | set(speed)
    tps_vals = [speed[m]["tps"] for m in models if speed.get(m, {}).get("tps")]
    lat_vals = [speed[m]["latency"] for m in models if speed.get(m, {}).get("latency")]
    tps_max = max(tps_vals) if tps_vals else 1.0
    lat_max = max(lat_vals) if lat_vals else 1.0
    w = cfg.get("weights", _DEFAULT_CONFIG["weights"])

    scores: dict[str, dict[str, Any]] = {}
    for m in models:
        q = quality.get(m, {})
        sp = speed.get(m, {})
        # neutral priors so a model with no data isn't unfairly buried (cold start)
        qual = q.get("avg_score") if q.get("avg_score") is not None else 0.7
        failed = q.get("failed_rate", 0.0)
        tps_n = (sp.get("tps") / tps_max) if (sp.get("tps") and tps_max) else 0.5
        lat_n = (sp.get("latency") / lat_max) if (sp.get("latency") and lat_max) else 0.5
        score = (w["quality"] * qual) + (w["throughput"] * tps_n) - (w["latency"] * lat_n) - (w["failure"] * failed)
        scores[m] = {"score": round(score, 4), "quality": round(qual, 3), "failed_rate": round(failed, 3),
                     "tps": sp.get("tps"), "latency": sp.get("latency")}

    # Per-worker scores, normalized within each model (compare replicas of the
    # same model to each other). Keyed (model, worker_id).
    wkeys = set(wq) | set(wp)
    by_model: dict[str, list[float]] = {}
    for (m, _wid) in wkeys:
        v = wp.get((m, _wid), {})
        if v.get("tps"):
            by_model.setdefault(m, []).append(v["tps"])
    wscores: dict[str, dict[str, Any]] = {}
    for (m, wid) in wkeys:
        q = wq.get((m, wid), {})
        p = wp.get((m, wid), {})
        qual = q.get("avg_score") if q.get("avg_score") is not None else 0.7
        failed = q.get("failed_rate", 0.0)
        tmax = max(by_model.get(m, [1.0]) or [1.0])
        lmax = max([wp[(m, k)]["latency"] for (mm, k) in wkeys if mm == m and wp.get((m, k), {}).get("latency")] or [1.0])
        tps_n = (p.get("tps") / tmax) if (p.get("tps") and tmax) else 0.5
        lat_n = (p.get("latency") / lmax) if (p.get("latency") and lmax) else 0.5
        sc = (w["quality"] * qual) + (w["throughput"] * tps_n) - (w["latency"] * lat_n) - (w["failure"] * failed)
        wscores[f"{m}|{wid}"] = {"score": round(sc, 4), "quality": round(qual, 3),
                                 "failed_rate": round(failed, 3), "tps": p.get("tps"), "latency": p.get("latency")}

    _SCORE_CACHE["scores"] = scores
    _SCORE_CACHE["workers"] = wscores
    _SCORE_CACHE["ts"] = time.time()


async def get_model_scores() -> dict[str, dict[str, Any]]:
    """Cached model scores; refresh lazily past the TTL. Never raises."""
    if time.time() - _SCORE_CACHE["ts"] > _SCORE_TTL:
        try:
            await _refresh_scores()
        except Exception as e:  # keep last-good / empty; scoring must not break routing
            logger.warning(f"model score refresh failed: {e}")
    return _SCORE_CACHE["scores"]


def pick_worker(model: str, workers: list[dict]) -> tuple[str, dict[str, Any]]:
    """Pick the best-scoring ONLINE worker replica serving `model`.

    Returns (worker_name, meta). Returns ("", ...) when there are 0/1 candidates
    (no benefit to pinning) or no worker scores yet — the normal dispatch then
    picks any worker. This is the fix for a single flaky replica: model scores
    well but one worker 502s, so we steer to the healthier replica.
    """
    cands = [w for w in workers if model in (w.get("models") or []) and w.get("online", True)]
    if len(cands) <= 1:
        return "", {"candidates": len(cands)}
    wscores = _SCORE_CACHE.get("workers", {})

    def _sc(w: dict) -> float:
        wid = w.get("worker_id") or w.get("id") or ""
        return wscores.get(f"{model}|{wid}", {}).get("score", 0.0)

    best = max(cands, key=_sc)
    return (best.get("name") or ""), {
        "candidates": len(cands),
        "worker": best.get("name"),
        "worker_score": round(_sc(best), 4),
    }


async def resolve_auto_async(model_field: str, prompt: str, available: list[str]) -> tuple[str, dict[str, Any]]:
    """Async entrypoint: fetch cached scores, then resolve. Use this from the API."""
    scores = await get_model_scores()
    return resolve_auto(model_field, prompt, available, scores=scores)


def resolve_auto(model_field: str, prompt: str, available: list[str], scores: dict | None = None) -> tuple[str, dict[str, Any]]:
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
    # Online candidates in curated order (deduped). Curation is the prior.
    seen: set[str] = set()
    online = [m for m in ordered if m in avail and not (m in seen or seen.add(m))]
    fallback = not online
    if not online:
        chosen = available[0] if available else model_field  # last resort
    elif scores:
        # Best-scoring online candidate. max() is stable on first-seen for ties, so
        # curated order breaks ties; live quality/speed can override the prior.
        chosen = max(online, key=lambda m: scores.get(m, {}).get("score", 0.0))
    else:
        chosen = online[0]  # Step-1 behavior: curated order, no scores

    meta = {
        "auto": True,
        "requested": model_field,
        "resolved_model": chosen,
        "task_class": task_class,
        "effort": eff,
        "gate_ms": round((time.time() - t0) * 1000, 2),
        "fallback": fallback,
        "scored": bool(scores),
    }
    if scores and chosen in scores:
        meta["score"] = scores[chosen].get("score")
    logger.info(
        f"auto route: {model_field} -> {chosen} (class={task_class} effort={eff} "
        f"fallback={fallback} {meta['gate_ms']}ms)"
    )
    return chosen, meta
