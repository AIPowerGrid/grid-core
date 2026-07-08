# recipes - curated ComfyUI workflows

## Purpose

Curated local recipe JSON loaded by `grid_api.services.recipes` at startup. These
serve as approved workflow definitions until or alongside RecipeVault sync.

**Adding a workflow? Start with `docs/architecture/RECIPE_DISPATCH.md`** — the
end-to-end guide (export → `recipe_import` CLI auto-detects the node map → fill
knobs → validate → drop here).

## Ownership

- `*.json` - ComfyUI workflow JSON with a `_grid` metadata block describing
  recipe name, job type, engine, required models, constraints, determinism, seed
  fields, and optional LoRA injection points.

## Local Contracts

- Recipe JSON is executable worker input. Treat it as code-like configuration.
- `_grid` metadata must remain consistent with `services/recipes.py` expectations.
- Do not silently widen user-controlled knobs. Parameters exposed through
  recipes become public API surface.
- If a recipe supports LoRAs, ensure `loras.py` and worker injection rules can
  enforce safe names, strengths, and injection points.

## Work Guidance

- Keep recipe names stable once public; clients may use them as model IDs.
- Validate imported or generated workflows with `services/recipe_import.py`
  conventions before adding them here.
- Prefer explicit constraints over worker-side defaults for size, seconds,
  sampler, scheduler, and seed behavior.

## Verification

- `pytest grid_api/services/tests/test_recipes.py`.

## Child DOX Index

- None - leaf.
