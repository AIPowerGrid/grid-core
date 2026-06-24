# horde/apis/v2 - legacy /api/v2 route registry

## Purpose

Concrete Flask-RESTX v2 resource implementations and route registration for
legacy Horde-compatible users, clients, and workers.

## Ownership

- `__init__.py` - central route registry:
  - image: `/generate/async`, `/generate/status/{id}`,
    `/generate/check/{id}`, `/generate/pop`, `/generate/submit`,
    `/generate/rate/{id}`;
  - text: `/generate/text/async`, `/generate/text/status/{id}`,
    `/generate/text/pop`, `/generate/text/submit`;
  - interrogation: `/interrogate/async`, `/interrogate/status/{id}`,
    `/interrogate/pop`, `/interrogate/submit`;
  - styles/collections: `/styles/*`, `/collections*`;
  - account/status/admin: `/users*`, `/workers*`, `/kudos/*`,
    `/status/*`, `/teams*`, `/operations/*`, `/filters*`,
    `/sharedkeys*`, `/documents/*`, `/auto_worker_type`.
- `base.py` - shared account, worker, kudos, model/status, filter, team,
  operations, document, and worker-message resources.
- `stable.py` - image generation and interrogation resources.
- `kobold.py` - text generation resources.
- `stable_styles.py`, `kobold_styles.py`, `styles.py` - legacy style and
  collection resources.

## Local Contracts

- `__init__.py` is the source of route truth for `/api/v2`. Keep it aligned with
  resource classes and models.
- Worker pop/submit routes must preserve queue semantics for existing workers.
- Status/check routes are frequently polled and should avoid expensive work.
- Admin/operations/filter routes require strict permission checks.
- New Grid-native behavior belongs in `grid_api/routers` unless legacy clients
  explicitly need it here.

## Work Guidance

- Do not rename legacy route paths without a deprecation and compatibility plan.
- When changing request/response shape, update `horde/apis/models/` and any
  README/API docs that describe the endpoint.
- Be suspicious of source image URLs, webhooks, worker-submitted fields, and
  operation/admin payloads.

## Verification

- `pytest tests/`.
- Manual route smoke may require Postgres, Redis, and Flask app setup.

## Child DOX Index

- None - leaf.
