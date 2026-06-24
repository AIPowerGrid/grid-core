# horde/apis - legacy /api/v2 resources

## Purpose

Flask-RESTX API layer for legacy Horde-compatible clients and workers, mounted
under `/api` with v2 resources registered in `v2/__init__.py`.

## Ownership

- `apiv2.py` - `/api` blueprint.
- `v2/__init__.py` - route registration for all `/api/v2` resources.
- `v2/base.py` - users, workers, kudos, status, models, teams, filters,
  operations, worker messages, documents, heartbeat, shared keys.
- `v2/stable.py` - image generation, image worker pop/submit, status/check,
  aesthetics, interrogation, image stats.
- `v2/kobold.py` - text generation, text worker pop/submit/status, text stats.
- `v2/*_styles.py`, `v2/styles.py` - legacy style and collection APIs.
- `models/` - request/response schemas for Flask-RESTX.

## Local Contracts

- Endpoint registrations in `v2/__init__.py` are the public contract. Keep docs,
  models, and implementation aligned when changing one.
- Worker pop/submit endpoints are queue-critical; preserve idempotency, timeout,
  and fault semantics.
- Admin/operation endpoints must remain permission-gated.
- Do not introduce new demand billing here unless it is explicitly part of the
  legacy compatibility plan; new billing belongs in `grid_api`.

## Work Guidance

- Change the resource model and resource handler together.
- Keep status/check routes cheap; they are polled frequently by clients.
- Be careful with route aliases and old client expectations. Prefer additive
  changes over shape changes.

## Verification

- `pytest tests/`.
- Manual Flask route smoke may require local Postgres/Redis.

## Child DOX Index

- [v2/AGENTS.md](v2/AGENTS.md) - legacy `/api/v2` route registry and resources.
