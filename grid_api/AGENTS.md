# grid_api — live v2 coordinator (FastAPI)

## Purpose

The running grid service: OpenAI/Anthropic `/v1` endpoints, worker WebSocket dispatch,
metering, quota/credits, and on-chain settlement. ~9.6K LOC. Entry point: `main.py`.

## Ownership

- `routers/` — HTTP + WS endpoints. Owned in its own AGENTS.md.
- `services/` — business logic (dispatch, economy, safety, settlement). Owned in its own AGENTS.md.
- `database.py` / `auth.py` / `ratelimit.py` / `format.py` — shared infrastructure (this doc).
- `v2/schema.py`, `models/` — request/response schemas.

## Local Contracts

- **Auth:** API keys are SHA-256 hashed; `auth.py` reimplements the hash to avoid importing
  the legacy horde — keep it byte-compatible with the dashboard's key generation.
- **DB:** only ever touch grid_api-owned tables; never the legacy horde tables.
- **Dispatch:** exactly one live job queue — `services/job_queue.py` (Redis streams). The
  `p2p/`, `waku_queue`, and `*_hybrid` variants are default-off scaffolding slated to be
  branched out (ADR-0001). Do not wire them into new code.
- **On-chain:** read via background sync loops in `main.py` (ModelVault); never on the hot path.

## Work Guidance

- Config: a typed `config.py` is the target; today env reads are scattered (~46 across the
  tree) — consolidate, don't add more ad-hoc `getenv`.
- Every router needs a contract test (most are currently untested).
- Errors: structured envelope; no bare `except:`.

## Verification

- `pytest grid_api/` — current tests cover the economic paths (job_queue, den, quota,
  settlement). Add router/auth/dispatch coverage with any change there.

## Child DOX Index

- [routers/AGENTS.md](routers/AGENTS.md) — HTTP + WebSocket endpoints.
- [services/AGENTS.md](services/AGENTS.md) — dispatch, economy, safety, settlement.
