# styles - curated style presets

## Purpose

Curated creative style presets loaded by `grid_api.services.styles` and exposed
via `GET /v1/styles`.

## Ownership

- `*.json` - style preset definitions: id/name, job type, target model/recipe,
  prompt template, negative prompt, locked/default params, and optional LoRAs.

## Local Contracts

- Styles compose over recipes; they must not bypass recipe constraints, LoRA
  checks, billing, quota, or safety gates.
- Prompt templates must include or intentionally omit the user prompt token as
  expected by `services/styles.py`.
- Style-provided LoRAs must pass the same validation as user-provided LoRAs.

## Work Guidance

- Keep style IDs stable once public.
- Prefer small, descriptive presets over hidden behavior changes that surprise
  users or workers.
- When adding a style for a new job type/model, verify the target workers and
  recipe constraints exist.

## Verification

- `pytest grid_api/services/tests/test_recipes.py`.
- Add style-specific tests when style parsing or application behavior changes.

## Child DOX Index

- None - leaf.
