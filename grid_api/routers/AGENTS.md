# grid_api/routers - HTTP + WebSocket endpoints

## Purpose

The grid's external surface. OpenAI/Anthropic-compatible inference, media gen, worker
transport, accounts, stats, health/metrics.

## Ownership

- `openai.py` - `POST /v1/chat/completions`, `GET /v1/models`,
  `GET /v1/models/{model_id}`. Sanitizes messages pre-dispatch, detects
  chat-routed media models, reserves text credits in live mode, and streams or
  collects worker output.
- `anthropic.py` - `POST /v1/messages` raw Anthropic Messages passthrough.
- `responses.py` - `POST /v1/responses` raw OpenAI Responses passthrough.
- `_passthrough.py` - shared raw passthrough submit/stream/collect and deep
  secret sanitization helpers.
- `images.py` - `POST /v1/images/generations` native image jobs.
- `videos.py` - `POST /v1/videos/generations` native video jobs.
- `worker_ws.py` - `/v1/workers/ws`: registration + dispatch + health/eviction + streaming.
  **God-file (~1.1K LOC); split target = registration / dispatch / health / stream.** Highest
  bug history (eviction cascade, idle-redelivery) - change carefully, add tests.
- `accounts.py` - wallet auth, dashboard/internal account/session creation,
  account profile, payout wallet, worker listing, API-key issue/revoke.
- `stats.py` - `GET /v1/workers`, progress polling, model status, usage totals,
  model stats, wallet earnings.
- `styles.py` - `GET /v1/styles` for curated creative presets.
- `health.py` - `GET /health`.
- `metrics.py` - `GET /metrics` Prometheus exposition.
- `tests/` - router-level tests, including billing/settlement behavior.

## Local Contracts

- Faithful passthrough: forward request/response shape unchanged except metering + sanitize.
- Every endpoint goes through the shared rate limiter (`ratelimit.py`) keyed by API key.
- Demand billing must be applied uniformly across all paid inference entry
  points before live charging. Do not add a new work-submitting route without
  reserve/reconcile or an explicit no-charge policy.
- `worker_ws.py` must not trust worker-reported counts for rewards or customer
  billing without a server-side cap or verification path.
- Media routes must pass `user.get("account_id")` to `services.media`; quota IDs
  like `v2:<uuid>` are not credit ledger account IDs.
- Worker affinity (`worker` request field) is ownership-gated before queueing.
- Public stats/health/metrics are unauthenticated by design; keep sensitive
  account/ledger details behind account auth.
- `accounts.py` internal-token routes are for trusted first-party services only.
  Any future bridge identity must use scoped keys plus signed assertions, not raw
  user headers.

## Work Guidance

- New endpoint -> add a contract test; wire auth + rate limit; route media via `services/media.py`,
  text via `services/job_queue` + `token_stream`.
- Prefer small helpers over expanding `worker_ws.py`. If a change affects worker
  registration, job dispatch, streaming, media, or health separately, consider a
  local extraction with tests.
- Preserve OpenAI/Anthropic error shapes where SDK compatibility depends on them.
- Keep request-size checks before sanitizer/tokenization for CPU and memory safety.

## Verification

- `pytest grid_api/routers/`.
- `pytest grid_api/services/tests/test_credits_billing.py` when changing any
  route that reserves, refunds, or reconciles credits.

## Child DOX Index

- `tests/` - router-level pytest coverage.
