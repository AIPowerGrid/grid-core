# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recipe authoring — turn a ComfyUI API-format workflow into a grid recipe.

Recipes are governed at the CORE (RecipeVault), not chosen by workers. This helper
ingests a ComfyUI workflow (a single file, or every file in a dir) and produces a
recipe-ready object: the graph + a `_grid` metadata block (engine, jobType, vars,
clamps, determinism, requiredModels).

It auto-detects the variable slots by *tracing the graph* (positive vs negative
prompt via the conditioning wiring; all seed inputs; the LoadImage start frame) and
returns any ambiguities as notes for the author to resolve. ComfyUI-specific
detection lives here; other engines get their own detector.
"""

import hashlib
import json
from typing import Any


def _ref(v: Any):
    """A ComfyUI input ref is [node_id, output_index]; return the node_id or None."""
    return v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) else None


def _trace_to_clip(wf: dict, start: str | None, depth: int = 0, seen=None) -> str | None:
    """Follow conditioning refs from `start` back to a CLIPTextEncode node id."""
    if start is None or depth > 8:
        return None
    seen = seen or set()
    if start in seen:
        return None
    seen.add(start)
    node = wf.get(start)
    if not node:
        return None
    if node.get("class_type") == "CLIPTextEncode":
        return start
    for k in ("positive", "negative", "conditioning", "cond", "guider", "model"):
        t = _trace_to_clip(wf, _ref(node.get("inputs", {}).get(k)), depth + 1, seen)
        if t:
            return t
    return None


def detect_vars(wf: dict) -> tuple[dict, list[str]]:
    """Best-effort detection of recipe var slots. Returns (vars, notes)."""
    vars: dict[str, Any] = {}
    notes: list[str] = []

    # prompt / negative_prompt — trace a node that splits positive vs negative.
    pos = neg = None
    for node in wf.values():
        ins = node.get("inputs", {})
        if _ref(ins.get("positive")) and _ref(ins.get("negative")):
            p = _trace_to_clip(wf, _ref(ins["positive"]))
            n = _trace_to_clip(wf, _ref(ins["negative"]))
            if p and n and p != n:
                pos, neg = p, n
                break
    clips = [nid for nid, node in wf.items() if node.get("class_type") == "CLIPTextEncode"]
    if pos:
        vars["prompt"] = f"{pos}.inputs.text"
    elif len(clips) == 1:
        vars["prompt"] = f"{clips[0]}.inputs.text"
    elif clips:
        notes.append(f"ambiguous positive prompt among CLIPTextEncode {clips} — set vars.prompt")
    if neg:
        vars["negative_prompt"] = f"{neg}.inputs.text"

    # seeds — every seed-ish input (often >1 for multi-pass); same value to all.
    seed_slots = [f"{nid}.inputs.{k}" for nid, node in wf.items()
                  for k in node.get("inputs", {}) if k in ("seed", "noise_seed")]
    if seed_slots:
        vars["seed"] = seed_slots if len(seed_slots) > 1 else seed_slots[0]

    # i2v start frame.
    imgs = [nid for nid, node in wf.items()
            if node.get("class_type") in ("LoadImage", "LoadImageOutput")]
    if imgs:
        vars["image"] = f"{imgs[0]}.inputs.image"
        if len(imgs) > 1:
            notes.append(f"multiple LoadImage nodes {imgs} — using {imgs[0]} for vars.image")

    return vars, notes


def build_recipe(wf: dict, name: str, *, job_type: str = "image", engine: str = "comfyui",
                 deterministic: bool = False, required_models: list[str] | None = None,
                 vars: dict | None = None, clamps: dict | None = None) -> tuple[dict, list[str]]:
    """Produce a recipe-ready workflow ({_grid, ...graph}) + detection notes.
    Author overrides (`vars`/`clamps`) win over auto-detection."""
    detected, notes = detect_vars(wf)
    final_vars = {**detected, **(vars or {})}
    grid = {
        "name": name, "engine": engine, "jobType": job_type, "deterministic": deterministic,
        "requiredModels": required_models or [], "vars": final_vars, "clamps": clamps or {},
    }
    return {"_grid": grid, **wf}, notes


def is_api_workflow(wf: Any) -> bool:
    """True if `wf` looks like a ComfyUI API-format graph (flat {id:{class_type,...}})."""
    return isinstance(wf, dict) and len(wf) > 0 and all(
        isinstance(v, dict) and "class_type" in v for v in wf.values())


def import_dir(path: str, *, job_type: str = "image", engine: str = "comfyui") -> dict:
    """Draft a recipe for every API-format *.json workflow in a directory.
    Returns {filename: (recipe, notes)}; the author reviews vars/jobType/determinism
    per recipe before storing to RecipeVault. (UI-format and non-workflow JSON skipped.)"""
    import os
    out: dict[str, tuple] = {}
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json"):
            continue
        try:
            wf = json.load(open(os.path.join(path, fn)))
        except (ValueError, OSError):
            continue
        if not is_api_workflow(wf):
            continue  # UI-format export or unrelated json — skip
        out[fn] = build_recipe(wf, os.path.splitext(fn)[0], job_type=job_type, engine=engine)
    return out


def validate_recipe(recipe_wf: dict) -> list[str]:
    """Structural lint for a recipe ({_grid, ...graph}) — catches authoring errors
    BEFORE they reach ComfyUI (the class of bug that silently breaks a model). Returns
    a list of problems (empty = valid). Pure/offline; safe to run in CI.

    Checks: every declared var path targets an existing node.input slot; every clamp/
    enum key is a declared var; every graph edge [node_id, idx] points to a real node;
    every node has a class_type.
    """
    problems: list[str] = []
    grid = recipe_wf.get("_grid") or {}
    graph = {k: v for k, v in recipe_wf.items() if k != "_grid"}
    vars_ = grid.get("vars") or {}

    def slot_exists(path: str) -> bool:
        cur: Any = graph
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return False
            cur = cur[p]
        return True

    for name, path in vars_.items():
        for p in (path if isinstance(path, list) else [path]):
            if not slot_exists(p):
                problems.append(f"var '{name}' targets missing slot '{p}'")
    for key in ("clamps", "enums"):
        for name in (grid.get(key) or {}):
            if name not in vars_:
                problems.append(f"{key} key '{name}' is not a declared var")
    for nid, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            problems.append(f"node '{nid}' missing class_type")
            continue
        for k, v in (node.get("inputs") or {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in graph:
                problems.append(f"node '{nid}'.{k} references missing node '{v[0]}'")
    return problems


def recipe_root(recipe_wf: dict) -> str:
    """Local content-hash id (sha256). On-chain RecipeVault computes its own keccak
    root at store time; this is the cache/registration key off-chain."""
    canon = json.dumps(recipe_wf, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(canon).hexdigest()
