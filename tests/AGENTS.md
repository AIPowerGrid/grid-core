# tests - legacy integration smoke tests

## Purpose

Top-level pytest suite for legacy Horde-compatible behavior and image/text smoke
tests. Grid v2 unit/router tests live under `grid_api/**/tests/`.

## Ownership

- `test_text.py` - text generation smoke behavior.
- `test_image.py`, `test_image_styles.py`, `test_image_extra_sources.py` -
  image and style smoke behavior.
- `test_alchemy.py` - alchemy/interrogation smoke behavior.
- `conftest.py` - shared top-level fixtures.

## Local Contracts

- These tests may skip when external services or credentials are unavailable.
- Do not add Grid v2 service tests here if they are better owned by
  `grid_api/services/tests` or `grid_api/routers/tests`.
- Keep tests deterministic; avoid real network dependencies unless explicitly
  marked/skipped.

## Work Guidance

- Prefer narrow tests near the code they cover.
- When preserving legacy compatibility, add tests here only if the behavior is
  top-level Flask/Horde behavior.

## Verification

- `pytest tests/`.
- Full `pytest` before changing shared auth, DB, or routing behavior.

## Child DOX Index

- None - leaf.
