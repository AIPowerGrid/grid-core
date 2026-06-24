# horde - legacy Flask/Horde compatibility runtime

## Purpose

Legacy Flask application and Horde-compatible `/api/v2` API. This still backs
legacy clients, registration pages, OAuth flows, public docs pages, and some
image/text queue compatibility while Grid API migration continues.

## Ownership

- `routes.py` - public Flask site routes, registration, OAuth redirects,
  static/proxy/debug helpers.
- `apis/` - Flask-RESTX `/api/v2` resource registration and API models.
- `classes/` - legacy ORM/domain classes for users, workers, waiting prompts,
  processing generations, teams, styles, and related objects.
- `database/` - legacy DB setup, thread helpers, and query/update functions.
- `blockchain/` and `model_reference_blockchain.py` - legacy model-registry
  integration.
- `image.py`, `r2.py` - legacy source-image handling and R2/S3 storage helpers.
- `validation.py`, `countermeasures.py`, `limiter.py` - legacy validation,
  abuse controls, and rate limiting.
- `templates/`, `data/` - Flask templates and content.

## Local Contracts

- This is compatibility code, not the preferred place for new Grid economics or
  worker settlement logic. Prefer `grid_api/` for new `/v1` behavior.
- Do not break `/api/v2` worker/client contracts without a migration plan.
- Legacy source-image URL fetching is a security-sensitive SSRF surface. Any
  change here needs explicit URL/IP/content-length behavior.
- Keep API key hashing compatible with `grid_api/auth.py` and the dashboard salt.
- Legacy SQL assumptions live in `sql_statements/`; coordinate DB changes there.

## Work Guidance

- For new public AI features, route through `grid_api` unless the user explicitly
  asks for legacy Horde compatibility.
- Remove or gate debug/test routes before hardening public Flask exposure.
- When touching legacy resource models, update `horde/apis/models/` and the
  matching resource implementation together.
- Treat webhook URLs, remote image URLs, OAuth inputs, and worker payloads as
  untrusted.

## Verification

- `pytest tests/` for legacy smoke tests.
- Full `pytest` if changes interact with shared DB/auth/config.

## Child DOX Index

- [apis/AGENTS.md](apis/AGENTS.md) - legacy `/api/v2` Flask-RESTX resources.
- [blockchain/AGENTS.md](blockchain/AGENTS.md) - legacy ModelRegistry client.
- [classes/AGENTS.md](classes/AGENTS.md) - legacy ORM/domain classes.
- [database/AGENTS.md](database/AGENTS.md) - legacy DB/session/thread helpers.
