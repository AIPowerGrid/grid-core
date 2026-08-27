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
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import get_settings
from ..safe_logging import error_type

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
    # Digest of the governed model-weight contract used for deterministic
    # validation. A checkpoint filename is not an identity and cannot unlock
    # fidelity probes. Empty for ordinary/non-deterministic recipes.
    model_digest: str = ""
    required_models: list[str] = field(default_factory=list)
    job_type: str = "image"          # image | video
    model_name: str = ""             # advertised model this recipe serves (≥1 recipe/model)
    lora_inject: Optional[dict] = None  # if set, recipe supports LoRAs (worker splices loaders here)
    seed_max: int = 2**53 - 1        # cap the seed to the model's range (e.g. TRELLIS = 2**31-1)


# recipe_root (lower hex) -> Recipe ; plus id + name indexes for convenience refs.
_BY_ROOT: dict[str, Recipe] = {}
_BY_ID: dict[int, Recipe] = {}
_BY_NAME: dict[str, Recipe] = {}   # lowercased name -> Recipe
# A model can have several recipes (e.g. a t2i and an i2i graph). modelName (lower)
# -> [recipes]; resolve_for_model picks the variant by whether a source frame exists.
_BY_MODEL: dict[str, list[Recipe]] = {}
_LOCAL_RECIPES: dict[str, Recipe] = {}
_ONCHAIN_RECIPES: dict[str, Recipe] = {}
_ONCHAIN_MASKED_ROOTS: set[str] = set()
_ONCHAIN_MASKED_NAMES: set[str] = set()
_ONCHAIN_SYNCED_AT: float | None = None
_ONCHAIN_FINALIZED_BLOCK: int | None = None
_ONCHAIN_FINALIZED_BLOCK_HASH: str | None = None
_CACHE_LOCK = threading.RLock()


# ── registry ─────────────────────────────────────────────────────────────────
def _recipe_from_workflow(
    recipe_root: str,
    name: str,
    workflow: dict,
    *,
    recipe_id: Optional[int] = None,
) -> Recipe:
    meta = dict(workflow.get("_grid") or {})
    spec = {k: v for k, v in workflow.items() if k != "_grid"}
    return Recipe(
        recipe_root=recipe_root.lower(),
        recipe_id=recipe_id,
        name=name,
        engine=str(meta.get("engine") or "comfyui"),
        spec=spec,
        vars=dict(meta.get("vars") or {}),
        clamps=dict(meta.get("clamps") or {}),
        enums=dict(meta.get("enums") or {}),
        deterministic=bool(meta.get("deterministic", False)),
        model_digest=str(meta.get("modelDigest") or "").lower(),
        required_models=list(meta.get("requiredModels") or []),
        job_type=str(meta.get("jobType") or "image"),
        model_name=str(meta.get("modelName") or name),
        lora_inject=(meta.get("loraInject") or None),
        seed_max=int(meta.get("seedMax") or (2**53 - 1)),
    )


def _index_recipe(r: Recipe) -> None:
    _BY_ROOT[r.recipe_root] = r
    _BY_NAME[r.name.lower()] = r
    if r.recipe_id is not None:
        _BY_ID[r.recipe_id] = r
    bucket = _BY_MODEL.setdefault(r.model_name.lower(), [])
    bucket[:] = [x for x in bucket if x.recipe_root != r.recipe_root] + [r]


def register_recipe(recipe_root: str, name: str, workflow: dict, *,
                    recipe_id: Optional[int] = None) -> Recipe:
    """Add/replace one process-local recipe.

    Startup file loading and chain synchronization use staged source snapshots;
    this direct helper remains for tests and explicit in-process registration.
    """
    r = _recipe_from_workflow(recipe_root, name, workflow, recipe_id=recipe_id)
    with _CACHE_LOCK:
        _index_recipe(r)
    return r


def _rebuild_source_cache_locked() -> None:
    """Rebuild all public indexes from local then verified on-chain sources."""
    _BY_ROOT.clear()
    _BY_ID.clear()
    _BY_NAME.clear()
    _BY_MODEL.clear()
    combined = {
        root: recipe
        for root, recipe in _LOCAL_RECIPES.items()
        if root not in _ONCHAIN_MASKED_ROOTS
        and recipe.name.lower() not in _ONCHAIN_MASKED_NAMES
    }
    combined.update(_ONCHAIN_RECIPES)  # verified chain entries intentionally win collisions
    for recipe in combined.values():
        _index_recipe(recipe)


def _replace_local_recipes(staged: dict[str, Recipe]) -> None:
    with _CACHE_LOCK:
        _LOCAL_RECIPES.clear()
        _LOCAL_RECIPES.update(staged)
        _rebuild_source_cache_locked()


def _install_onchain_snapshot(staged: dict[str, Recipe], snapshot) -> None:
    global _ONCHAIN_SYNCED_AT, _ONCHAIN_FINALIZED_BLOCK, _ONCHAIN_FINALIZED_BLOCK_HASH
    with _CACHE_LOCK:
        if _ONCHAIN_FINALIZED_BLOCK is not None:
            if snapshot.finalized_block < _ONCHAIN_FINALIZED_BLOCK:
                raise ValueError("RecipeVault snapshot would roll back finalized authority")
            if (
                snapshot.finalized_block == _ONCHAIN_FINALIZED_BLOCK
                and snapshot.finalized_block_hash != _ONCHAIN_FINALIZED_BLOCK_HASH
            ):
                raise ValueError("RecipeVault finalized block hash changed at the installed height")
        _ONCHAIN_RECIPES.clear()
        _ONCHAIN_RECIPES.update(staged)
        _ONCHAIN_MASKED_ROOTS.clear()
        _ONCHAIN_MASKED_ROOTS.update(record.recipe_root.lower() for record in snapshot.records)
        _ONCHAIN_MASKED_NAMES.clear()
        _ONCHAIN_MASKED_NAMES.update(record.name.lower() for record in snapshot.records)
        _ONCHAIN_SYNCED_AT = time.monotonic()
        _ONCHAIN_FINALIZED_BLOCK = snapshot.finalized_block
        _ONCHAIN_FINALIZED_BLOCK_HASH = snapshot.finalized_block_hash
        _rebuild_source_cache_locked()


def _clear_onchain_authority() -> None:
    global _ONCHAIN_SYNCED_AT, _ONCHAIN_FINALIZED_BLOCK, _ONCHAIN_FINALIZED_BLOCK_HASH
    with _CACHE_LOCK:
        _ONCHAIN_RECIPES.clear()
        _ONCHAIN_MASKED_ROOTS.clear()
        _ONCHAIN_MASKED_NAMES.clear()
        _ONCHAIN_SYNCED_AT = None
        _ONCHAIN_FINALIZED_BLOCK = None
        _ONCHAIN_FINALIZED_BLOCK_HASH = None
        _rebuild_source_cache_locked()


def _expire_stale_onchain_recipes() -> None:
    """Drop stale chain authority while retaining reviewed local fallbacks."""
    global _ONCHAIN_SYNCED_AT
    max_stale = get_settings().recipevault_max_stale_seconds
    if not 60 <= max_stale <= 86_400:
        max_stale = 1800
    with _CACHE_LOCK:
        if (
            _ONCHAIN_SYNCED_AT is not None
            and time.monotonic() - _ONCHAIN_SYNCED_AT > max_stale
        ):
            _ONCHAIN_RECIPES.clear()
            _ONCHAIN_SYNCED_AT = None
            _rebuild_source_cache_locked()
            logger.error(
                "RecipeVault cache expired; chain-governed recipes failed closed"
            )


# File extensions that denote model weights (for worker preflight file checks).
_MODEL_EXTS = (".safetensors", ".ckpt", ".pth", ".pt", ".bin", ".gguf", ".onnx")


def node_types(spec: dict) -> list[str]:
    """Distinct ComfyUI node class_types in a recipe graph — a worker must have all
    of these installed to run the recipe (preflight tier 1)."""
    return sorted({v.get("class_type") for v in spec.values()
                   if isinstance(v, dict) and v.get("class_type")})


def model_files(spec: dict) -> list[str]:
    """Weight files a recipe graph references (loader-node string inputs that look
    like model files) — a worker must have all of these on disk (preflight tier 2)."""
    files = set()
    for v in spec.values():
        if not isinstance(v, dict):
            continue
        for iv in (v.get("inputs") or {}).values():
            if isinstance(iv, str) and iv.lower().endswith(_MODEL_EXTS):
                files.add(iv)
    return sorted(files)


def get_recipe(ref: str | int) -> Optional[Recipe]:
    """Look up by recipe_root (hex str), recipe_id (int/numeric str), or name."""
    _expire_stale_onchain_recipes()
    with _CACHE_LOCK:
        if isinstance(ref, int):
            return _BY_ID.get(ref)
        s = str(ref)
        if s.lower() in _BY_ROOT:
            return _BY_ROOT[s.lower()]
        if s.isdigit() and int(s) in _BY_ID:
            return _BY_ID[int(s)]
        return _BY_NAME.get(s.lower())


def list_recipes() -> list[Recipe]:
    _expire_stale_onchain_recipes()
    with _CACHE_LOCK:
        return list(_BY_ROOT.values())


def recipes_for_model(ref: str | int) -> list[Recipe]:
    """All recipes serving a model (by modelName), else a single by-name/root/id hit."""
    _expire_stale_onchain_recipes()
    with _CACHE_LOCK:
        out = _BY_MODEL.get(str(ref).lower())
        if out:
            return list(out)
    r = get_recipe(ref)
    return [r] if r else []


def generation_modes(ref: str | int) -> list[str]:
    """Client-facing generation modes derived from approved recipe variants."""
    modes: set[str] = set()
    for recipe in recipes_for_model(ref):
        if recipe.job_type not in ("image", "video"):
            continue
        if recipe.job_type == "image":
            mode = "img2img" if "image" in recipe.vars else "txt2img"
        else:
            mode = "img2video" if "image" in recipe.vars else "txt2video"
        modes.add(mode)
    order = ("txt2img", "img2img", "txt2video", "img2video")
    return [mode for mode in order if mode in modes]


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
    from .recipe_import import recipe_root

    staged: dict[str, Recipe] = {}
    if not os.path.isdir(dir_path):
        return 0
    for fn in sorted(os.listdir(dir_path)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, fn)) as recipe_file:
                wf = json.load(recipe_file)
        except (ValueError, OSError):
            continue
        if not isinstance(wf, dict) or "_grid" not in wf:
            continue  # not a recipe (raw workflow / unrelated)
        name = (wf.get("_grid") or {}).get("name") or os.path.splitext(fn)[0]
        root = recipe_root(wf)
        staged[root.lower()] = _recipe_from_workflow(root, name, wf)
    _replace_local_recipes(staged)
    n = len(staged)
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


def _get_path(spec: dict, path: str) -> Any:
    """Read a declared recipe slot without mutating the graph."""
    cur: Any = spec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise RecipeError(f"slot '{path}' targets a missing path")
        cur = cur[part]
    return cur


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
    # Cap to the recipe's seed_max — some models (e.g. TRELLIS) reject seeds above
    # 2**31-1; a supplied over-range seed is folded (mod), never rejected.
    smax = r.seed_max
    seed = inputs.get("seed")
    if seed in (None, ""):
        seed = secrets.randbelow(smax + 1)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise RecipeError(f"'seed' must be an integer, got {seed!r}")
    if seed < 0:
        raise RecipeError("'seed' must be non-negative")
    if seed > smax:
        seed = seed % (smax + 1)
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
        "model_digest": r.model_digest,
        "seed": inputs["seed"],
        "required_models": r.required_models,
        "lora_inject": r.lora_inject,   # worker splices LoraLoader nodes here (if loras requested)
        "image_paths": r.vars.get("image"),  # path(s) the worker binds a source image to (image slot is NOT pre-injected)
        "spec": spec,
    }


def _recipe_for_model(model: str, inputs: dict | None = None, *,
                      has_source: bool = False) -> Optional[Recipe]:
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

    return (next((r for r in cands if _matches(r)), None)
            or next((r for r in cands if ("image" in r.vars) == has_source), None)
            or cands[0])


def baked_default_for_model(model: str, input_name: str, inputs: dict | None = None, *,
                            has_source: bool = False) -> Any:
    """Return a recipe's baked value for a declared client input.

    Billing uses this when a client omits a deterministic unit such as video
    seconds. It must charge the graph that will actually run, not a global guess.
    """
    chosen = _recipe_for_model(model, inputs, has_source=has_source)
    if not chosen:
        return None
    path = chosen.vars.get(input_name)
    if not path:
        return None
    first_path = path[0] if isinstance(path, list) else path
    return _get_path(chosen.spec, first_path)


def resolve_for_model(model: str, inputs: dict | None = None, *, has_source: bool = False) -> Optional[dict]:
    """Media-layer entry point: pick the right recipe for `model` and resolve it to a
    concrete graph spec; else return None so the caller falls back to legacy dispatch.

    Variant selection: a model may have a text-only (t2i) recipe AND an image-input
    (i2i / edit) recipe. When the job carries a source frame (`has_source`), prefer
    the recipe that declares an `image` var; otherwise prefer the one that doesn't.
    Falls back to whatever recipe exists if there's no exact match (e.g. LTX i2v,
    whose only recipe takes an image but runs a baked default frame when none given).
    `inputs` may be the raw payload — only declared vars present get injected."""
    chosen = _recipe_for_model(model, inputs, has_source=has_source)
    if not chosen:
        return None
    return resolve(chosen.recipe_root, inputs)


# ── on-chain sync (off the hot path; no-op until configured) ──────────────────
async def sync_from_recipevault() -> int:
    """Atomically install one verified, public RecipeVault snapshot.

    The feature is explicitly default-off. Missing configuration or any chain,
    quorum, runtime, content, or policy error leaves the last-known-good cache
    untouched.
    """
    settings = get_settings()
    if not settings.recipevault_sync_enabled:
        if _ONCHAIN_RECIPES or _ONCHAIN_MASKED_ROOTS:
            _clear_onchain_authority()
        return 0
    try:
        import asyncio

        return await asyncio.to_thread(_sync_from_recipevault_blocking, settings)
    except Exception as exc:
        logger.error("RecipeVault sync failed error_type=%s", error_type(exc))
        _expire_stale_onchain_recipes()
        return 0


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("recipe JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"recipe JSON contains non-finite number {value}")


def _stage_onchain_recipes(snapshot) -> dict[str, Recipe]:
    """Parse and policy-check the complete snapshot without mutating caches."""
    from .recipe_import import recipe_root, validate_recipe

    staged: dict[str, Recipe] = {}
    ids: set[int] = set()
    names: set[str] = set()
    for record in snapshot.records:
        if not record.is_public:
            continue
        if record.compression != 0:
            raise ValueError("public governed recipes must be uncompressed")
        raw = bytes(record.workflow_data)
        workflow = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
        if not isinstance(workflow, dict):
            raise ValueError("recipe JSON must be an object")
        canonical = json.dumps(
            workflow,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if raw != canonical:
            raise ValueError("recipe bytes are not Core-canonical JSON")
        computed_root = "0x" + hashlib.sha256(canonical).hexdigest()
        if computed_root != record.recipe_root.lower() or recipe_root(workflow) != computed_root:
            raise ValueError("recipe root does not commit to Core-canonical JSON")

        meta = workflow.get("_grid")
        if not isinstance(meta, dict):
            raise ValueError("recipe is missing object _grid metadata")
        if not isinstance(meta.get("vars"), dict):
            raise ValueError("recipe vars must be an object")
        if not isinstance(meta.get("clamps", {}), dict) or not isinstance(meta.get("enums", {}), dict):
            raise ValueError("recipe clamps and enums must be objects")
        if str(meta.get("engine") or "") != "comfyui":
            raise ValueError("on-chain recipe engine is not supported")
        if str(meta.get("jobType") or "") not in {"image", "video", "3d"}:
            raise ValueError("on-chain recipe job type is not supported")
        if not record.name or len(record.name) > 128 or meta.get("name") != record.name:
            raise ValueError("on-chain and content recipe names must match")
        model_name = meta.get("modelName")
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 128:
            raise ValueError("recipe modelName is required and bounded")
        required_models = meta.get("requiredModels")
        if (
            not isinstance(required_models, list)
            or not 1 <= len(required_models) <= 8
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in required_models)
        ):
            raise ValueError("recipe requiredModels must contain 1-8 bounded names")
        deterministic = meta.get("deterministic") is True
        model_digest = str(meta.get("modelDigest") or "").lower()
        if deterministic and not re.fullmatch(r"[0-9a-f]{64}", model_digest):
            raise ValueError("deterministic recipes require a governed modelDigest")
        if record.can_create_nfts and not deterministic:
            raise ValueError("NFT-enabled recipes must be deterministic")
        problems = validate_recipe(workflow)
        if problems:
            raise ValueError("recipe failed structural validation")
        if record.recipe_id <= 0 or record.recipe_id in ids:
            raise ValueError("recipe ids must be positive and unique")
        name_key = record.name.lower()
        if name_key in names or computed_root in staged:
            raise ValueError("recipe names and roots must be unique")
        try:
            recipe = _recipe_from_workflow(
                computed_root,
                record.name,
                workflow,
                recipe_id=record.recipe_id,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("recipe metadata cannot be normalized") from exc
        ids.add(record.recipe_id)
        names.add(name_key)
        staged[computed_root] = recipe
    return staged


def _sync_from_recipevault_blocking(settings) -> int:
    """Read, verify, stage, then atomically replace the on-chain source."""
    from .recipe_vault_sync import read_quorum_recipe_snapshot, reviewed_runtime_hash

    runtime_hash = reviewed_runtime_hash(settings.recipevault_verifier_version)
    if runtime_hash is None:
        raise ValueError("RecipeVault verifier version is not reviewed")
    primary = settings.base_rpc_url.get_secret_value() if settings.base_rpc_url else ""
    confirmation = (
        settings.recipevault_confirmation_rpc_url.get_secret_value()
        if settings.recipevault_confirmation_rpc_url
        else ""
    )
    snapshot = read_quorum_recipe_snapshot(
        rpc_url=primary,
        confirmation_rpc_url=confirmation,
        expected_chain_id=settings.recipevault_chain_id,
        diamond_address=settings.recipevault_address,
        expected_facet_runtime_hash=runtime_hash,
        max_records=settings.recipevault_max_records,
        max_workflow_bytes=settings.recipevault_max_workflow_bytes,
        rpc_timeout_seconds=settings.recipevault_rpc_timeout_seconds,
        max_finalized_age_seconds=settings.recipevault_max_finalized_age_seconds,
    )
    staged = _stage_onchain_recipes(snapshot)
    _install_onchain_snapshot(staged, snapshot)
    logger.info(
        "RecipeVault sync installed %d public recipe(s) at finalized block %d",
        len(staged),
        snapshot.finalized_block,
    )
    return len(staged)
