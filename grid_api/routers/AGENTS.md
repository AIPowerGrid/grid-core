# grid_api/routers — HTTP + WebSocket endpoints

## Purpose

The grid's external surface. OpenAI/Anthropic-compatible inference, media gen, worker
transport, accounts, stats, health/metrics.

## Ownership

- `openai.py` — `/v1/chat/completions` (streaming), `/v1/models`. Sanitizes messages
  (secrets) pre-dispatch.
- `anthropic.py` — `/v1/messages`. `responses.py` — OpenAI Responses API.
- `images.py` — `/v1/images`. `videos.py` — `/v1/videos`.
- `worker_ws.py` — `/v1/workers/ws`: registration + dispatch + health/eviction + streaming.
  **God-file (~1.1K LOC); split target = registration / dispatch / health / stream.** Highest
  bug history (eviction cascade, idle-redelivery) — change carefully, add tests.
- `accounts.py` — account + API-key management. `stats.py` — model/worker/usage stats.
- `health.py`, `metrics.py` — probes + Prometheus. `_passthrough.py` — shared passthrough +
  `deep_sanitize`.

## Local Contracts

- Faithful passthrough: forward request/response shape unchanged except metering + sanitize.
- Every endpoint goes through the shared rate limiter (`ratelimit.py`) keyed by API key.
- **Safety gate (planned, blocking for public):** content filter must run pre-dispatch here
  and on outputs — see `docs/architecture/SAFETY_MODEL.md`. `sanitizer.py` today is secrets-only.

## Work Guidance

- New endpoint → add a contract test; wire auth + rate limit; route media via `services/media.py`,
  text via `services/job_queue` + `token_stream`.

## Verification

- `pytest` (router tests to be added — currently thin here).

## Child DOX Index

- None — leaf.
