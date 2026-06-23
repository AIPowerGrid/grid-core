# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LoRA request handling — CivitAI passthrough with a validator-governed blacklist.

Decision: open CivitAI sourcing (not a curated allow-list), gated by a BLACKLIST of
banned LoRA ids / weight-hashes. The blacklist is fed by validator consensus + a
coordinator backstop (see docs/architecture/LORA_DISPATCH.md); this module is the
synchronous hot-path gate — no validator call in the request path.

P1 scope: schema validation + strength/count gating + capability gate + blacklist
check. The worker download/hash-verify/graph-injection is P2; validator scan is P3.
"""

import json
import logging
import os
import re
from fastapi import HTTPException

from . import recipes

logger = logging.getLogger("grid_api.loras")

MAX_LORAS = 5
STRENGTH_MIN, STRENGTH_MAX = -2.0, 2.0  # default; per-recipe/ModelVault may tighten
# A LoRA name must be a CivitAI id/version (digits) or a simple filename token — NEVER a
# URL or path. The worker turns this into a CivitAI download; allowing URLs/paths here =
# worker-side SSRF / arbitrary fetch. Enforce the shape at the grid (the trust boundary).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# Blacklist: banned by CivitAI id/name (lowercased) and by pinned weight-hash. Seeded
# from a local file now (coordinator backstop), validator-fed via blacklist_add later.
_BLACKLIST_IDS: set[str] = set()
_BLACKLIST_HASHES: set[str] = set()


def load_blacklist(path: str) -> int:
    """Load {"ids": [...], "hashes": [...]} from a JSON file if present. Returns count."""
    if not os.path.isfile(path):
        return 0
    try:
        data = json.load(open(path))
    except (ValueError, OSError) as e:
        logger.warning("LoRA blacklist load failed (%s)", e)
        return 0
    _BLACKLIST_IDS.update(str(x).lower() for x in (data.get("ids") or []))
    _BLACKLIST_HASHES.update(str(x).lower() for x in (data.get("hashes") or []))
    n = len(_BLACKLIST_IDS) + len(_BLACKLIST_HASHES)
    logger.info("LoRA blacklist: %d id(s), %d hash(es)", len(_BLACKLIST_IDS), len(_BLACKLIST_HASHES))
    return n


def blacklist_add(*, lora_id: str | None = None, weight_hash: str | None = None) -> None:
    """Add an entry (validator-consensus or coordinator backstop feed). Idempotent."""
    if lora_id:
        _BLACKLIST_IDS.add(str(lora_id).lower())
    if weight_hash:
        _BLACKLIST_HASHES.add(str(weight_hash).lower())


def is_blacklisted(name: str, weight_hash: str | None = None) -> bool:
    if str(name).lower() in _BLACKLIST_IDS:
        return True
    return bool(weight_hash) and str(weight_hash).lower() in _BLACKLIST_HASHES


def _strength(entry: dict, key: str, default: float = 1.0) -> float:
    raw = entry.get(key, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"lora '{key}' must be a number, got {raw!r}")
    if v < STRENGTH_MIN or v > STRENGTH_MAX:
        raise HTTPException(status_code=422,
                            detail=f"lora '{key}' must be between {STRENGTH_MIN} and {STRENGTH_MAX}")
    return v


def prepare_loras(model: str, loras: list | None) -> list[dict]:
    """Validate + normalize a request's `loras` for a model. Returns the normalized list
    the worker will download/inject (empty if none requested). Rejects (422) on: model
    not LoRA-capable, too many, bad strength, or a blacklisted LoRA — so a banned or
    unsupported LoRA never silently no-ops."""
    if not loras:
        return []
    if not isinstance(loras, list):
        raise HTTPException(status_code=422, detail="loras must be a list")
    if not recipes.supports_loras(model):
        raise HTTPException(status_code=422, detail=f"model '{model}' does not support loras")
    if len(loras) > MAX_LORAS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_LORAS} loras per request")

    out: list[dict] = []
    for entry in loras:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            raise HTTPException(status_code=422, detail="each lora needs a non-empty 'name'")
        name = str(entry["name"]).strip()
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=422,
                                detail="lora 'name' must be a CivitAI id or simple filename (no URLs/paths)")
        if is_blacklisted(name):
            raise HTTPException(status_code=422, detail=f"lora '{name}' is not allowed")
        out.append({
            "name": name,
            "model": _strength(entry, "model"),
            "clip": _strength(entry, "clip"),
            "is_version": bool(entry.get("is_version", False)),
            "inject_trigger": (str(entry["inject_trigger"]) if entry.get("inject_trigger") else None),
        })
    return out
