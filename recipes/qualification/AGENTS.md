# recipes/qualification - non-serving candidate workflows

## Purpose

Versioned media candidates retained for testing, not startup advertisement.
`load_local_recipes()` reads only the direct `recipes/*.json` children. Never
make it recursive: placing a workflow here must not activate it.

The BF16 and NVFP4 Distilled graphs now also have exact promoted copies in `recipes/`
for the bounded credit pilot. Keep those copies byte-semantically identical;
no Dev/Ingredients promotion follows. The retained candidates are not loaded
recursively, and inventory alone does not enable job admission.

## Ownership

- `ltx-2.5-*-v1.json` owns the single-stage BF16 distilled/dev, text/image-pin
  qualification graphs. They mirror the locally rendered native workflows.
- `docs/architecture/LTX_2_5_VIDEO.md` owns evidence and promotion gates.
- `ltx-2.5-dev-bf16-ingredients-v1.json` is the separate fixed 121-frame
  appearance/source candidate, not a replacement for standard Dev or I2V.
- The two `ltx-2.5-distilled-nvfp4-*-v1.json` candidates retain the Distilled
  schedule and source scaffold with a distinct model/name/root and official
  NVFP4 backbone. Encoder/VAEs stay BF16. Exact startup copies prepare only
  the owner-approved bounded credit canary after native generated/reference
  qualification. Fresh CI, deployment, enrollment and purchased-credit proof
  remain gates; these copies cannot open admission or legacy waiting routes.

## Contracts

- Keep customer model IDs distinct from LTX-2.3 and between dev/distilled.
- NVFP4 is a separate identity, never a caller-selected precision or BF16
  alias. Pricing/inventory cannot authorize public enrollment or source modes.
- No arbitrary executable nodes, filename selectors, precision switches,
  LoRAs or timeline JSON from clients. Typed image pins may expand
  only the Core-owned first-image scaffold; images bind through declared slots.
  Distilled graphs now declare audio/video source modes for qualification.
  Only Core's fixed source expansion can add the frozen-audio and official
  Union-Canny branches. Input WAV/MP4 bytes are normalized and independently
  validated upstream, hash-bound to roles and worker slots, never ignored.
  Opening images remain explicit/optional; no appearance-only identity claim.
  New source admission and public worker advertisement remain disabled.
- Distilled BF16 candidate ceiling is481 frames; NVFP4 remains337 and standard
  dev81. The BF16 extension needs native execution and release qualification;
  it is not a claim of every workload fitting GPU memory. Ingredients dev is
  fixed to 768x448/121/40 steps/CFG2 with no opening pins. Its appearance PNG
  binds only into Core's Ingredients expansion; source audio is mandatory and
  motion video optional. It has its own model ID, default-dark admission and
  worker feature, with no public price or identity guarantee. Text-only graphs accept no
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
- `pytest grid_api/services/tests/test_ltx25_appearance.py`
  Set `MEDIA_VIDEO_WORKER_SOURCE` to a reviewed checkout with a test `.venv`
  for the optional actual worker-adapter handoff (synthetic/mocked HTTP only).
- `pytest grid_api/services/tests/test_ltx25_billing.py`; set a disposable
  `CREDITS_TEST_DB_URL` for the PostgreSQL race cases. Each case removes only
  its own temporary schema. Required release CI uses PostgreSQL 16.
- `pytest grid_api/routers/tests/test_ltx25_video_contract.py`
- `python -m grid_api.services.recipe_import --validate <candidate.json>`
- `git diff --check`

## Child DOX Index

- None - leaf.
