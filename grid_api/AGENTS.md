# grid_api - live v2 coordinator (FastAPI)

## Purpose

The running Grid service: OpenAI/Anthropic-compatible `/v1` endpoints, worker
WebSocket dispatch, media generation, metering, quota/credits, den ledger, Base
chain sync, and settlement scaffolding. Entry point: `main.py`.

## Ownership

- `routers/` - HTTP + WS endpoints. Owned in its own AGENTS.md.
- `services/` - business logic (dispatch, economy, safety, settlement). Owned in its own AGENTS.md.
- `database.py` / `auth.py` / `safe_logging.py` / `ratelimit.py` / `format.py` -
  shared infrastructure (this doc). `safe_logging.py` owns keyed opaque
  identifiers and bounded exception metadata for operational logs.
- `v2/` - grid-owned SQLAlchemy schema. Owned in its own AGENTS.md.
- `models/` - Pydantic request/response models for OpenAI-compatible requests
  and worker structures.
- `abis/` / `_abi.py` - local contract ABI loaders used by background sync.
- `main.py` - lifecycle: DB/Redis init, stale-job reclaimer, reservation and
  billing invariant monitors, validator operational-history pruning,
  default-off finalized worker-bond sync, operator alerts, recipe sync, router
  registration, and root health metadata.

## Local Contracts

- **Auth:** API keys are SHA-256 hashed; keep `auth.py` byte-compatible with
  server-side key issuance. Retired `users.api_key` credentials are not an
  authentication authority.
- **DB:** runtime code touches only Grid-owned tables. Keep `v2/schema.py` and
  Alembic in lockstep; historical tables are read-only archaeology.
- **Dispatch:** exactly one live job queue - `services/job_queue.py` (Redis streams). The
  `services/p2p/` variants are default-off scaffolding and must not become the
  production path without a dedicated design/test pass.
- **On-chain:** read via background sync loops or offline jobs, cached; never
  perform Base RPC calls on the hot request path.
- The validator bond loop may update only existing reviewed reference rows. It
  verifies one finalized block, the configured Diamond address, all expected
  selector routes, and the routed facet runtime hash before one atomic refresh.
- **Billing:** live charging must reserve before dispatch and reconcile/refund
  after terminal job state. Add tests for every endpoint that moves paid work.
- **Safety:** `services/sanitizer.py` is secret redaction, not a content safety
  system. Do not treat it as CSAM/PII/NSFW moderation.
- **Media capabilities:** `/v1/status/models` publishes generation modes derived
  from approved recipe variants. A connected checkpoint alone does not authorize
  `img2img` or `img2video`; the corresponding recipe must declare an image input.

## Work Guidance

- Config: a typed `config.py` is the target; feature-specific env reads remain
  scattered across the tree. Keep `deploy/env.template` current and consolidate
  rather than adding another ad-hoc `getenv`.
- Every router needs a contract test. Existing coverage is strongest in services
  and billing helper paths; route/worker interop remains the risky seam.
- Errors: structured envelope; no bare `except:`.
- Logs must not contain raw account, wallet, job, identity, credential, or
  payment identifiers, nor exception messages that may embed request or SQL
  values. Use `safe_logging.opaque_id` for correlation and
  `safe_logging.error_type` for failure classification.
- Preserve faithful passthrough behavior unless the endpoint contract explicitly
  says the Grid mutates shape for metering, sanitizing, or media abstraction.

## Verification

- `pytest grid_api/`.
- `pytest grid_api/services/` for service/economic changes.
- `pytest grid_api/routers/` for endpoint, billing, or worker transport changes.

## Child DOX Index

- [routers/AGENTS.md](routers/AGENTS.md) - HTTP + WebSocket endpoints.
- [services/AGENTS.md](services/AGENTS.md) - dispatch, economy, safety, settlement.
- [v2/AGENTS.md](v2/AGENTS.md) - grid-owned SQLAlchemy schema.
