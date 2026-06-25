# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recipe resolver — the governed graph layer for media generation.

A *recipe* is an on-chain-approved ComfyUI workflow (RecipeVault on Base). Clients
never send graphs; they pick a recipe (by name/root) and supply inputs (prompt,
seed, image, dims). This module:

  - caches approved recipes (synced from RecipeVault, off the hot path),
  - resolves a recipe + client inputs into a concrete ComfyUI graph to dispatch.

Recipe metadata (which node slots are variable, clamp ranges, determinism, required
models, job type) rides in a `_grid` block inside the stored workflow JSON — so v1
needs ZERO contract change (the contract already stores the workflow). See
docs/architecture/RECIPE_DISPATCH.md.

SECURITY: inputs are injected into *parsed* node-input slots, never string-formatted
into the JSON. A prompt full of quotes/braces is just a dict value — it cannot alter
graph structure. Only recipes present in the cache (i.e. approved) can be resolved.
"""

import copy
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("grid_api.recipes")


@dataclass
class Recipe:
    recipe_root: str                 # bytes32 hex (content hash) — canonical id
    recipe_id: Optional[int]         # convenience alias (RecipeVault sequential id)
    name: str
    engine: str                      # "comfyui" | "drawthings" | "native-ltx" | …
    spec: dict                       # engine-specific executable (ComfyUI graph, DT params, …)
    vars: dict[str, Any]             # input name -> dotted path (str) or list of paths (e.g. dual seed)
    clamps: dict[str, list]          # numeric input name -> [lo, hi]
    enums: dict[str, list] = field(default_factory=dict)  # input name -> allowed values (reject off-list)
    deterministic: bool = False
    required_models: list[str] = field(default_factory=list)
    job_type: str = "image"          # image | video
    model_name: str = ""             # advertised model this recipe serves (≥1 recipe/model)
    lora_inject: Optional[dict] = None  # if set, recipe supports LoRAs (worker splices loaders here)


# recipe_root (lower hex) -> Recipe ; plus id + name indexes for convenience refs.
_BY_ROOT: dict[str, Recipe] = {}
_BY_ID: dict[int, Recipe] = {}
_BY_NAME: dict[str, Recipe] = {}   # lowercased name -> Recipe
# A model can have several recipes (e.g. a t2i and an i2i graph). modelName (lower)
# -> [recipes]; resolve_for_model picks the variant by whether a source frame exists.
_BY_MODEL: dict[str, list[Recipe]] = {}


# ── registry ─────────────────────────────────────────────────────────────────
def register_recipe(recipe_root: str, name: str, workflow: dict, *,
                    recipe_id: Optional[int] = None) -> Recipe:
    """Add/replace a recipe in the cache. `workflow` is the full stored graph,
    including its `_grid` metadata block (which is split out here)."""
    meta = dict(workflow.get("_grid") or {})
    spec = {k: v for k, v in workflow.items() if k != "_grid"}
    r = Recipe(
        recipe_root=recipe_root.lower(),
        recipe_id=recipe_id,
        name=name,
        engine=str(meta.get("engine") or "comfyui"),
        spec=spec,
        vars=dict(meta.get("vars") or {}),
        clamps=dict(meta.get("clamps") or {}),
        enums=dict(meta.get("enums") or {}),
        deterministic=bool(meta.get("deterministic", False)),
        required_models=list(meta.get("requiredModels") or []),
        job_type=str(meta.get("jobType") or "image"),
        model_name=str(meta.get("modelName") or name),
        lora_inject=(meta.get("loraInject") or None),
    )
    _BY_ROOT[r.recipe_root] = r
    _BY_NAME[name.lower()] = r
    if recipe_id is not None:
        _BY_ID[recipe_id] = r
    bucket = _BY_MODEL.setdefault(r.model_name.lower(), [])
    bucket[:] = [x for x in bucket if x.recipe_root != r.recipe_root] + [r]
    return r


def get_recipe(ref: str | int) -> Optional[Recipe]:
    """Look up by recipe_root (hex str), recipe_id (int/numeric str), or name."""
    if isinstance(ref, int):
        return _BY_ID.get(ref)
    s = str(ref)
    if s.lower() in _BY_ROOT:
        return _BY_ROOT[s.lower()]
    if s.isdigit() and int(s) in _BY_ID:
        return _BY_ID[int(s)]
    return _BY_NAME.get(s.lower())


def list_recipes() -> list[Recipe]:
    return list(_BY_ROOT.values())


def recipes_for_model(ref: str | int) -> list[Recipe]:
    """All recipes serving a model (by modelName), else a single by-name/root/id hit."""
    out = _BY_MODEL.get(str(ref).lower())
    if out:
        return list(out)
    r = get_recipe(ref)
    return [r] if r else []


def supports_loras(ref: str | int) -> bool:
    """True if ANY recipe for the model declares a `loraInject` block — i.e. it has a
    graph injection point for LoRA loaders. The recipe is the capability authority; a
    model without one rejects `loras` rather than silently dropping them."""
    return any(r.lora_inject for r in recipes_for_model(ref))


def lora_inject_for(ref: str | int) -> Optional[dict]:
    """The loraInject spec for the LoRA-capable recipe of a model (else None)."""
    for r in recipes_for_model(ref):
        if r.lora_inject:
            return r.lora_inject
    return None


def supports_image(ref: str | int) -> bool:
    """True if ANY recipe for the model declares an `image` var — i.e. the model
    accepts an input frame (img2img / img2video). The recipe is the source of truth
    for capability; a model with no such recipe rejects source images rather than
    silently ignoring them."""
    return any("image" in r.vars for r in recipes_for_model(ref))


def supports_denoise(ref: str | int) -> bool:
    """True if ANY recipe for the model declares a `denoise` var — a latent-blend
    img2img *strength* knob (low denoise = stay close to the source). FLUX.2-style
    reference/edit recipes have no such slot (edit influence is conditioning-based),
    so a model without it rejects `strength`/`denoise` rather than silently ignoring
    it — same capability-gate contract as supports_image / supports_loras."""
    return any("denoise" in r.vars for r in recipes_for_model(ref))


# Recipe var names whose client-facing param name differs (the request uses the
# OpenAI-ish `cfg_scale`; the graph slot is `cfg`).
_CLIENT_PARAM_NAME = {"cfg": "cfg_scale"}


def param_schema(ref: str | int) -> Optional[dict]:
    """Client-facing parameter schema for a model, derived from its recipe(s).

    Returns None when no recipe serves the model (e.g. a text model). Merges the
    UNION of vars across a model's variants (t2i + i2i/edit), so `image` shows up
    for a model that has an edit recipe. Numeric knobs carry their gated [min,max]
    band (from clamps), categorical knobs their allow-list — i.e. exactly what the
    resolver will accept (out-of-band → 422, never silently clamped). The caller
    layers on global media limits (size / n / output_format) not encoded per-recipe.
    """
    cands = recipes_for_model(ref)
    if not cands:
        return None
    params: dict[str, dict] = {}
    for r in cands:
        for var in r.vars:
            name = _CLIENT_PARAM_NAME.get(var, var)
            if name in params:
                continue
            if var == "image":
                params[name] = {"type": "image",
                                "description": "img2img / edit source — inline base64 or data: URI"}
            elif var in r.clamps:
                lo, hi = r.clamps[var][0], r.clamps[var][1]
                params[name] = {"type": "number", "minimum": lo, "maximum": hi}
            elif var in r.enums:
                params[name] = {"type": "enum", "options": list(r.enums[var])}
            elif var in ("prompt", "negative_prompt"):
                params[name] = {"type": "string", "max_length": _MAX_PROMPT_CHARS}
                if var == "prompt":
                    params[name]["required"] = True
            elif var == "seed":
                params[name] = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}
            else:
                params[name] = {"type": "number"}
    return {
        "model": cands[0].model_name,
        "job_type": cands[0].job_type,
        "capabilities": {
            "img2img": any("image" in r.vars for r in cands),
            "loras": any(r.lora_inject for r in cands),
            "strength": any("denoise" in r.vars for r in cands),
        },
        "params": params,
    }


def load_local_recipes(dir_path: str) -> int:
    """Register curated recipes from local `*.json` files (each a {_grid, ...graph}).
    For v1 / pre-RecipeVault: drop a recipe in the dir and it's servable at startup.
    Returns the number loaded. Name comes from `_grid.name` (else the filename)."""
    import os
    from .recipe_import import recipe_root
    n = 0
    if not os.path.isdir(dir_path):
        return 0
    for fn in sorted(os.listdir(dir_path)):
        if not fn.endswith(".json"):
            continue
        try:
            wf = json.load(open(os.path.join(dir_path, fn)))
        except (ValueError, OSError):
            continue
        if not isinstance(wf, dict) or "_grid" not in wf:
            continue  # not a recipe (raw workflow / unrelated)
        name = (wf.get("_grid") or {}).get("name") or os.path.splitext(fn)[0]
        register_recipe(recipe_root(wf), name, wf)
        n += 1
    logger.info("Loaded %d local recipe(s) from %s", n, dir_path)
    return n


# ── resolution (the safe part) ───────────────────────────────────────────────
class RecipeError(Exception):
    """Recipe not found/approved, or inputs invalid."""


def _set_path(spec: dict, path: str, value: Any) -> None:
    """Set a value at a dotted path into the parsed spec — engine-neutral.
    ComfyUI: '3.inputs.seed' (nested). Draw Things / flat engines: 'seed'.
    Operates on the parsed dict (never string substitution); the final key must
    already exist (a recipe can only fill declared slots, not invent structure)."""
    parts = path.split(".")
    cur: Any = spec
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise RecipeError(f"slot '{path}' targets a missing path")
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        raise RecipeError(f"slot '{path}' targets a missing field")
    cur[parts[-1]] = value


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _validate_num(name: str, value: Any, lo: float, hi: float) -> float | int:
    """Range-GATE a numeric knob: reject (don't silently clamp) anything outside
    the allowed band, so a caller learns their request was invalid instead of
    quietly getting different output. Omitted knobs never reach here (they keep the
    recipe's baked default) — only explicitly-supplied values are gated."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise RecipeError(f"'{name}' must be a number, got {value!r}")
    if v < lo or v > hi:
        raise RecipeError(f"'{name}' must be between {_fmt(lo)} and {_fmt(hi)}")
    return int(v) if float(v).is_integer() else v


def _validate_enum(name: str, value: Any, allowed: list) -> str:
    """Allow-LIST a categorical knob (sampler/scheduler): reject anything not in the
    curated set, since a bad sampler produces garbage rather than just a variation.
    The set is per-recipe (mirrors on-chain ModelVault allowedSamplers/Schedulers)."""
    v = str(value)
    if v not in allowed:
        raise RecipeError(f"'{name}' must be one of: {', '.join(map(str, allowed))}")
    return v


def _range_for(recipe: "Recipe", name: str) -> Optional[list]:
    """The allowed [lo, hi] band for a numeric knob — the SINGLE seam where the
    range source lives. The on-chain ModelVault `ModelConstraints` (per model) is
    the intended source of truth; until that's wired (MODELVAULT_ADDRESS), the
    recipe's own `clamps` define the band. Step 2 only changes this function:
        c = model_constraints.get(recipe.required_models, name)
        if c is not None: return c
    """
    return recipe.clamps.get(name)


# Inputs that are images must be a grid-issued upload id/ref, never an arbitrary
# URL (kills SSRF via inputs). The dispatch layer resolves the id to bytes.
_MAX_PROMPT_CHARS = 8000


def resolve(ref: str | int, inputs: dict | None = None) -> dict:
    """Resolve an approved recipe + client inputs into a concrete dispatch spec.

    Returns: {recipe_root, name, job_type, deterministic, seed, graph, required_models}
    Raises RecipeError if the recipe isn't approved/cached or inputs are invalid.
    """
    inputs = dict(inputs or {})
    r = get_recipe(ref)
    if r is None:
        raise RecipeError(f"recipe '{ref}' is not approved / not in the vault")

    spec = copy.deepcopy(r.spec)

    # Seed: first-class. Default to a fresh one; always echo back (for NFT repro).
    seed = inputs.get("seed")
    if seed in (None, ""):
        seed = secrets.randbelow(2**53)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise RecipeError(f"'seed' must be an integer, got {seed!r}")
    if seed < 0 or seed > 2**53 - 1:
        raise RecipeError(f"'seed' must be between 0 and {2**53 - 1}")
    inputs["seed"] = seed

    for name, path in r.vars.items():
        if name not in inputs or inputs[name] is None:
            continue  # absent/None input keeps the recipe's baked default slot value
        val = inputs[name]
        rng = _range_for(r, name)
        if rng is not None:                       # numeric, range-gated (reject if out of band)
            val = _validate_num(name, val, rng[0], rng[1])
        elif name in r.enums:                     # categorical, allow-listed (reject off-list)
            val = _validate_enum(name, val, r.enums[name])
        elif name in ("prompt", "negative_prompt"):
            val = str(val)[:_MAX_PROMPT_CHARS]
        # image inputs: caller must pass a grid upload ref; validated upstream.
        # A var may target one slot (str) or several (list) — e.g. a seed fed to
        # multiple sampling passes, set identically for reproducibility.
        for p in (path if isinstance(path, list) else [path]):
            _set_path(spec, p, val)

    return {
        "recipe_root": r.recipe_root,
        "recipe_id": r.recipe_id,
        "name": r.name,
        "engine": r.engine,
        "job_type": r.job_type,
        "deterministic": r.deterministic,
        "seed": inputs["seed"],
        "required_models": r.required_models,
        "lora_inject": r.lora_inject,   # worker splices LoraLoader nodes here (if loras requested)
        "spec": spec,
    }


def resolve_for_model(model: str, inputs: dict | None = None, *, has_source: bool = False) -> Optional[dict]:
    """Media-layer entry point: pick the right recipe for `model` and resolve it to a
    concrete graph spec; else return None so the caller falls back to legacy dispatch.

    Variant selection: a model may have a text-only (t2i) recipe AND an image-input
    (i2i / edit) recipe. When the job carries a source frame (`has_source`), prefer
    the recipe that declares an `image` var; otherwise prefer the one that doesn't.
    Falls back to whatever recipe exists if there's no exact match (e.g. LTX i2v,
    whose only recipe takes an image but runs a baked default frame when none given).
    `inputs` may be the raw payload — only declared vars present get injected."""
    cands = recipes_for_model(model)
    if not cands:
        return None
    # Variant selection. A model may have up to three recipes: t2i (no image),
    # an edit/reference i2i (image, no denoise), and a latent-blend i2i (image +
    # denoise/strength). Route by the request: a source frame WITH a denoise/
    # strength knob → the blend recipe; a source frame alone → the edit recipe;
    # no source → t2i. Falls back to the old image-presence match, then any recipe.
    inputs = inputs or {}
    wants_denoise = has_source and inputs.get("denoise") is not None

    def _matches(r) -> bool:
        has_img = "image" in r.vars
        if has_img != has_source:
            return False
        if has_img and ("denoise" in r.vars) != bool(wants_denoise):
            return False
        return True

    chosen = (next((r for r in cands if _matches(r)), None)
              or next((r for r in cands if ("image" in r.vars) == has_source), None)
              or cands[0])
    return resolve(chosen.recipe_root, inputs)


# ── on-chain sync (off the hot path; no-op until configured) ──────────────────
async def sync_from_recipevault() -> int:
    """Pull approved recipes from RecipeVault into the cache. Returns count synced.
    No-ops (returns 0) if BASE_RPC_URL / RECIPEVAULT_ADDRESS aren't set."""
    addr = os.getenv("RECIPEVAULT_ADDRESS") or os.getenv("GRID_DIAMOND_ADDRESS")
    rpc = os.getenv("BASE_RPC_URL")
    if not addr or not rpc:
        logger.info("RecipeVault not configured (RECIPEVAULT_ADDRESS/BASE_RPC_URL) — "
                    "cache has %d seeded recipe(s)", len(_BY_ROOT))
        return 0
    try:
        from web3 import Web3  # noqa: F401  (import-availability check only)
    except Exception as e:
        logger.warning("RecipeVault sync deps unavailable (%s) — cache unchanged", e)
        return 0
    try:
        # web3 RPC is synchronous + does N sequential network round-trips; run it OFF the
        # event loop or it stalls token streaming + worker dispatch for the whole sync.
        import asyncio
        return await asyncio.to_thread(_sync_from_recipevault_blocking, addr, rpc)
    except Exception as e:
        logger.error("RecipeVault sync failed: %s", e)
        return 0


def _sync_from_recipevault_blocking(addr: str, rpc: str) -> int:
    """Synchronous web3 pull — MUST run via asyncio.to_thread (never on the loop)."""
    from web3 import Web3
    from .._abi import RECIPEVAULT_ABI, decompress_workflow
    w3 = Web3(Web3.HTTPProvider(rpc))
    c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=RECIPEVAULT_ABI)
    total = c.functions.totalRecipes().call()
    n = 0
    for rid in range(1, total + 1):
        # getRecipe -> (recipeId, recipeRoot, workflowData, creator,
        #               canCreateNFTs, isPublic, compression, createdAt, name, description)
        rec = c.functions.getRecipe(rid).call()
        recipe_id, recipe_root, workflow_data = rec[0], rec[1], rec[2]
        compression, name = rec[6], rec[8]
        root_hex = recipe_root.hex() if isinstance(recipe_root, (bytes, bytearray)) else str(recipe_root)
        root_hex = root_hex if root_hex.startswith("0x") else "0x" + root_hex
        workflow = json.loads(decompress_workflow(workflow_data, compression).decode("utf-8"))
        register_recipe(root_hex, name, workflow, recipe_id=int(recipe_id))
        n += 1
    logger.info("RecipeVault sync: %d recipes cached", n)
    return n
