# grid_api/services - dispatch, economy, safety, settlement

## Purpose

Business logic behind the routers: job dispatch, token streaming, the on-chain economy,
content sanitization, and reward settlement.

## Ownership

- **Dispatch:** `job_queue.py` (Redis streams - the ONE live queue), `token_stream.py`
  (worker->client token relay), `media.py` (image/video job abstraction), `storage.py`
  (presigned R2 upload), `enforcement.py` (worker strike/evict).
- **Economy:** `credits.py`, `quota.py` (free-tier daily), `pricing.py`, `ledger.py`,
  `den.py` (den accounting), `accounts.py`, `model_registry.py` (ModelVault sync).
- **Validation evidence:** `validators.py` issues validator assignments, verifies
  assignment-bound attestations, computes non-economic quorum state, and builds
  aggregate scorecards. Authoritative evidence must match the Grid-issued
  assignment id, nonce, and hard-targeted probe evidence hash. It must not route
  production jobs, reward, slash, or write worker ledger rows.
- **Model/media governance:** `recipes.py`, `recipe_import.py`, `styles.py`,
  `loras.py`, `model_registry.py`.
- **Safety:** `sanitizer.py` - **secrets redactor only** (strips API keys/PGP from prompts).
  NOT a content filter.
- **Settlement:** `settlement/` - owned in its own AGENTS.md.
- **Deferred decentralized dispatch:** `p2p/` - owned in its own AGENTS.md and
  default-off.
- **Tests:** `tests/` - service-level pytest coverage.

## Local Contracts

- One queue: `job_queue.py`. Requeue is capped (Redis counter, dead-letter at the cap) to
  prevent poison-job eviction cascades. Stale jobs reclaimed by the loop in `main.py`.
- Money paths must stay idempotent and tested; value-moving credit ledger writes
  require non-null refs and must not overdraft under concurrency.
- Media billing reserves exact deterministic cost before dispatch and refunds on
  non-running paths; text billing reserves max cost and reconciles against trusted
  usage.
- `ledger.py` writes one completion event per job. Settlement and stats depend on
  `grid_ledger`; do not revive orphan den tables for new v2 payouts.
- On-chain reads only via sync loops, cached; never per-request.
- `model_registry.py` is not currently wired into startup. Do not claim
  ModelVault enforcement is live unless the sync is wired and tested.
- `enforcement.py` records slashable evidence only; it must not directly slash
  bonded funds from a hot request path.
- Validator attestations and scorecards are evidence only until reward/dispute
  rules exist. A submitted or aggregated `failed` verdict is not a worker strike
  by itself.
- Authoritative validator evidence requires a Grid-issued assignment id, nonce,
  and matching probe evidence hash. Preview/local evidence stays visible only as
  preview.
- Validator attestation identity is evidence identity only, but must still be
  coherent: malformed validator wallet strings are rejected, signed evidence
  requires a claimed wallet, and stored validator wallets are normalized
  lowercase.

## Work Guidance

- Adding economic logic -> add/extend tests under `tests/` or `settlement/tests/`.
- Safety work should be a layered pre/post-dispatch content policy; do not
  overload `sanitizer.py`.
- When adding env-driven behavior, prefer centralizing in `grid_api/config.py`
  over scattered `os.getenv`.
- Keep synchronous Web3/R2/network work off the event loop; use startup loops,
  offline jobs, or `asyncio.to_thread` as appropriate.

## Verification

- `pytest grid_api/services/` - covers `job_queue`, `den`, `quota` (+ settlement subtree).

## Child DOX Index

- [p2p/AGENTS.md](p2p/AGENTS.md) - default-off P2P decentralization prototype.
- [settlement/AGENTS.md](settlement/AGENTS.md) - Merkle settlement + IPFS + aggregation.
- `tests/` - service unit tests (job_queue, den, quota).
