# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Styles — curated creative presets that compose OVER recipes.

A *model* is weights, a *recipe* is how to execute a model (graph + clamp/enum
governance), and a *style* is a user-facing creative preset: a prompt template,
optional LoRAs, the good parameter values, and which of those params are LOCKED
(not user-overridable — e.g. `steps` on a step-distilled model like FLUX.2 klein,
where 4 is the trained value and more just overcooks the image).

Styles never bypass governance: after a style is applied the request still flows
through the recipe resolver, whose clamps/enums are the hard gate. The style is
the friendly layer on top.

Source of truth is the grid (served at GET /v1/styles) so every surface — gallery,
chat, raw API, aigarth — shares one curated set. Loaded from local JSON now;
a future on-chain StyleVault can replace the loader without touching callers.

Style JSON shape (one file per style under styles/):
    {
      "id": "photoreal",
      "name": "Photoreal",
      "description": "...",
      "model": "FLUX.2 Klein 4B FP8",      # intended model (sets request model if unset)
      "job_type": "image",                  # image | video
      "prompt": "{prompt}, photorealistic, 8k, natural lighting",
      "negative_prompt": "cartoon, blurry, lowres",
      "params": {"steps": 4, "cfg_scale": 1.0, "sampler": "euler"},
      "locked": ["steps", "cfg_scale"],     # user cannot override these
      "loras": [{"name": "123456", "model": 0.8}],   # CivitAI, gated by loras svc
      "aspect": "3:4"                        # optional UX hint
    }
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("grid_api.styles")

# {prompt} placeholder in a style's prompt template gets the caller's prompt.
_PROMPT_TOKEN = "{prompt}"


@dataclass
class Style:
    id: str
    name: str
    description: str = ""
    model: str = ""
    job_type: str = "image"
    prompt: str = _PROMPT_TOKEN
    negative_prompt: str = ""
    params: dict = field(default_factory=dict)
    locked: list = field(default_factory=list)
    loras: list = field(default_factory=list)
    aspect: str = ""


_BY_ID: dict[str, Style] = {}


def register_style(d: dict) -> Optional[str]:
    sid = str(d.get("id") or "").strip().lower()
    if not sid:
        return None
    _BY_ID[sid] = Style(
        id=sid,
        name=str(d.get("name") or sid),
        description=str(d.get("description") or ""),
        model=str(d.get("model") or ""),
        job_type=str(d.get("job_type") or "image"),
        prompt=str(d.get("prompt") or _PROMPT_TOKEN),
        negative_prompt=str(d.get("negative_prompt") or ""),
        params=dict(d.get("params") or {}),
        locked=list(d.get("locked") or []),
        loras=list(d.get("loras") or []),
        aspect=str(d.get("aspect") or ""),
    )
    return sid


def load_local_styles(dir_path: str) -> int:
    """Register curated styles from local `*.json` files. Returns the count."""
    n = 0
    if not os.path.isdir(dir_path):
        logger.info("no styles dir at %s", dir_path)
        return 0
    for fn in sorted(os.listdir(dir_path)):
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(dir_path, fn)))
        except (ValueError, OSError) as e:
            logger.warning("skip style %s: %s", fn, e)
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        if register_style(d):
            n += 1
    logger.info("loaded %d style(s) from %s", n, dir_path)
    return n


def get_style(style_id: str) -> Optional[Style]:
    return _BY_ID.get(str(style_id or "").strip().lower())


def list_styles(job_type: Optional[str] = None) -> list[dict]:
    """Public listing for GET /v1/styles. Hides nothing secret — these are presets."""
    out = []
    for s in _BY_ID.values():
        if job_type and s.job_type != job_type:
            continue
        out.append({
            "id": s.id, "name": s.name, "description": s.description,
            "model": s.model, "job_type": s.job_type, "aspect": s.aspect,
            # surface which knobs are fixed so a UI can hide/lock them
            "locked": s.locked,
        })
    return out


def apply_style(style_id: str, *, prompt: str, user_params: dict,
                user_negative: str = "") -> dict:
    """Expand a style into effective generation inputs.

    Returns {model, prompt, negative_prompt, params, loras}. Caller merges these
    into the request, then runs the normal recipe path (which clamps everything).

    - prompt: the style template with {prompt} replaced by the caller's prompt.
      If the template has no token, the caller's prompt is appended.
    - params: style defaults, overlaid by user_params EXCEPT for `locked` keys,
      which always take the style's value (a distilled model's steps can't be
      cranked into overcooked output).
    - negative_prompt: style's, plus the user's appended (both honored).
    """
    s = get_style(style_id)
    if s is None:
        raise KeyError(style_id)

    p = (prompt or "").strip()
    eff_prompt = s.prompt.replace(_PROMPT_TOKEN, p) if _PROMPT_TOKEN in s.prompt \
        else (f"{p}, {s.prompt}".strip(", ") if s.prompt else p)

    neg_parts = [x for x in (s.negative_prompt, (user_negative or "").strip()) if x]
    eff_negative = ", ".join(neg_parts)

    # start from style params, let user override the UNLOCKED ones
    locked = {k.lower() for k in s.locked}
    params = dict(s.params)
    for k, v in (user_params or {}).items():
        if v is None:
            continue
        if k.lower() in locked:
            continue  # locked: style wins, user override ignored
        params[k] = v

    return {
        "model": s.model,
        "prompt": eff_prompt,
        "negative_prompt": eff_negative,
        "params": params,
        "loras": list(s.loras),
        "locked": sorted(locked),
    }
