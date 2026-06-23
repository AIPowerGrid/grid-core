# grid_api/services — dispatch, economy, safety, settlement

## Purpose

Business logic behind the routers: job dispatch, token streaming, the on-chain economy,
content sanitization, and reward settlement.

## Ownership

- **Dispatch:** `job_queue.py` (Redis streams — the ONE live queue), `token_stream.py`
  (worker→client token relay), `media.py` (image/video job abstraction), `storage.py`
  (presigned R2 upload), `enforcement.py` (worker strike/evict).
- **Economy:** `credits.py`, `quota.py` (free-tier daily), `pricing.py`, `ledger.py`,
  `den.py` (den accounting), `accounts.py`, `model_registry.py` (ModelVault sync).
- **Safety:** `sanitizer.py` — **secrets redactor only** (strips API keys/PGP from prompts).
  NOT a content filter; CSAM/NSFW/IP-abuse is an unbuilt blocking gap (SAFETY_MODEL.md).
- **Settlement:** `settlement/` — owned in its own AGENTS.md.
- **Deferred scaffolding (do not extend):** `p2p/`, `waku_queue.py`, `job_queue_hybrid.py`,
  `p2p/hybrid_queue.py` — default-off, slated to branch out (ADR-0001).

## Local Contracts

- One queue: `job_queue.py`. Requeue is capped (Redis counter, dead-letter at the cap) to
  prevent poison-job eviction cascades. Stale jobs reclaimed by the loop in `main.py`.
- Money paths must stay idempotent and tested; they are the parts that already have tests.
- On-chain reads only via sync loops, cached; never per-request.

## Work Guidance

- Adding economic logic → add/extend tests under `tests/` or `settlement/tests/`.
- Safety work → implement the layered model in SAFETY_MODEL.md; do not overload `sanitizer.py`.

## Verification

- `pytest grid_api/services/` — covers `job_queue`, `den`, `quota` (+ settlement subtree).

## Child DOX Index

- [settlement/AGENTS.md](settlement/AGENTS.md) — Merkle settlement + IPFS + aggregation.
- `tests/` — service unit tests (job_queue, den, quota).
