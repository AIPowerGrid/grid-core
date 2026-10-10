# recipes/qualification - non-serving candidate workflows

## Purpose

Versioned media candidates retained for testing, not startup advertisement.
`load_local_recipes()` reads only the direct `recipes/*.json` children. Never
make it recursive: placing a workflow here must not activate it.

## Ownership

- `ltx-2.5-*-v1.json` owns the single-stage BF16 distilled/dev, text/image-pin
  qualification graphs. They mirror the locally rendered native workflows.
- `docs/architecture/LTX_2_5_VIDEO.md` owns evidence and promotion gates.

## Contracts

- Keep customer model IDs distinct from LTX-2.3 and between dev/distilled.
- No arbitrary executable nodes, filename selectors, precision switches,
  LoRAs, audio input or timeline JSON from clients. Typed image pins may expand
  only the Core-owned first-image scaffold; images bind through declared slots.
- Distilled ceiling is 337 frames; dev remains 81. Text-only graphs accept no
  pins; image graphs permit up to sixteen strictly increasing pins starting at
  zero, within the clip, with finite strengths in 0..1. These ceilings describe
  the bounded candidate, not exhaustive quality qualification.
- Preserve the exact 8-step distilled sigma schedule and CFG 1.
- Graph frame count, audio frame count and all three FPS slots must agree.
- `_grid.videoContract` must match graph vars, clamps, defaults and components.
- An admission ceiling is not proof of quality or safety at every combination.
- Promotion requires reviewed pricing, ordinary durable billing, worker
  execution/storage and recovery canaries; a successful local render is not
  production approval. Keep dev and multi-guide quality experimental.

## Verification

- `pytest grid_api/services/tests/test_ltx25_contract.py`
- `pytest grid_api/services/tests/test_ltx25_billing.py`; set a disposable
  `CREDITS_TEST_DB_URL` for the PostgreSQL race cases. Each case removes only
  its own temporary schema. Required release CI uses PostgreSQL 16.
- `pytest grid_api/routers/tests/test_ltx25_video_contract.py`
- `python -m grid_api.services.recipe_import --validate <candidate.json>`
- `git diff --check`

## Child DOX Index

- None - leaf.
