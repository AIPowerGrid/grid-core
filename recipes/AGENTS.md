# recipes - curated ComfyUI workflows

## Purpose

Curated local recipe JSON loaded by `grid_api.services.recipes` at startup. These
serve as approved workflow definitions until or alongside RecipeVault sync.

**Adding a workflow? Start with `docs/architecture/RECIPE_DISPATCH.md`** — the
end-to-end guide (export → `recipe_import` CLI auto-detects the node map → fill
knobs → validate → drop here).

## Ownership

- MOSS Image SFT BF16 has exact startup and qualification copies after native
  text/source execution and all three existing image-model regressions pass.
  These prepare the reviewed worker/billing canary, not public worker capacity
  or deployment approval. Keep its actual latent-img2img contract distinct from
  identity/reference editing; see `qualification/MOSS_IMAGE.md` for remaining gates.
- `*.json` - ComfyUI workflow JSON with a `_grid` metadata block describing
  recipe name, job type, engine, required models, constraints, determinism, seed
  fields, and optional LoRA injection points.
- The four `ltx-2.5-distilled-{bf16,nvfp4}-*-v1.json` files are exact copies of the
  reviewed qualification graphs, promoted for the owner-approved bounded
  credit pilot. Startup exposes their inventory; new jobs still require the
  async enable/cohort/expiry, compatible worker, paid hold and generation gates.
  Dev/Ingredients remain only in the nonrecursive qualification directory.
  The distinct NVFP4 copies prepare the owner-only purchased-credit canary
  after local generated/image/audio/video qualification, not public admission.
  Both models remain blocked from legacy waiting/chat in pilot mode.
  The BF16 duration-extension candidate raises only its frame clamp/contract
  to625; its qualification copies match. NVFP4 stays337. Defaults, precision,
  pixel limits and admission are unchanged. Native long-clip and release checks
  must pass before deploying the larger bound.

## Local Contracts

- Recipe JSON is executable worker input. Treat it as code-like configuration.
- `_grid` metadata must remain consistent with `services/recipes.py` expectations.
- Do not silently widen user-controlled knobs. Parameters exposed through
  recipes become public API surface.
- If a recipe supports LoRAs, ensure `loras.py` and worker injection rules can
  enforce safe names, strengths, and injection points.
- Client generation modes come from recipe variants: a recipe with an `image`
  var contributes `img2img`/`img2video`; one without it contributes
  `txt2img`/`txt2video`. Never advertise a mode by metadata alone.

## Work Guidance

- Keep recipe names stable once public; clients may use them as model IDs.
- Validate imported or generated workflows with `services/recipe_import.py`
  conventions before adding them here.
- Prefer explicit constraints over worker-side defaults for size, seconds,
  sampler, scheduler, and seed behavior.

## Verification

- `pytest grid_api/services/tests/test_recipes.py`.

## Child DOX Index

- [qualification/AGENTS.md](qualification/AGENTS.md) - non-serving LTX-2.5
  candidate recipes and promotion gates. Startup loading must stay nonrecursive.
