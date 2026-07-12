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
- `accounts.py` - native Google/SIWE auth, bounded service exchange, and
  default-off legacy dashboard/internal session creation,
  account profile (incl. resolved `payout{asset, aipg_bps, active, live_asset}`),
  payout wallet + `POST /v1/account/payout-preference` (both SESSION-gated),
  worker listing, API-key issue/revoke, `GET /v1/account/credits` (promotional/free/paid
  pockets; `total_spendable_*` = what can pay NOW vs `total_preview_*`;
  `free.active` tracks GRID_FREE_SPENDABLE_LIVE), `GET /v1/account/jobs`
  (operator trust view: my workers' jobs + den + result_hash + signed flag,
  scoped to the payout wallet), deposit claims (USDC + Chainlink-priced ETH).
  `POST /v1/accounts/session` is the retired internal-token bridge. It
  resolves on exactly one authoritative identity (`oauth_sub` first, then
  wallet, then verified email only when it is the sole identity); supplemental
  or unverified email must never join accounts.
  Native service/app exchange lives at `/v1/auth/service/exchange`; Google ID
  tokens are verified at `/v1/auth/google/exchange`; `/v1/auth/service/bind`
  binds an app subject after recent Google/SIWE proof. `/v1/accounts/bridges`
  bootstraps a bounded service client only when separately enabled.
- `stats.py` - `GET /v1/workers`, progress polling, model status, usage totals,
  model stats, wallet earnings, `GET /v1/payouts/public` (aggregate payout
  transparency), `GET /v1/jobs/recent` (PUBLIC redacted job feed: model, worker
  handle, timing, den, prompt/result hashes + signed flag — NEVER content,
  NEVER customer wallet/account).
- `validator.py` - validator assignment-bound evidence surface:
  `GET /v1/validator/capabilities`, `GET /v1/validator/assignments`,
  `POST /v1/validator/probe/{assignment_id}`,
  `POST /v1/validator/attest`, `GET /v1/validator/workers`,
  `GET /v1/validator/scorecards`, and
  `GET /v1/validator/assignments/health`. This enables targeted evidence
  collection, but still has no routing/reward/slash authority.
- `styles.py` - `GET /v1/styles` for curated creative presets.
- `health.py` - `GET /health`.
- `metrics.py` - `GET /metrics` Prometheus exposition.
- `tests/` - router-level tests, including billing/settlement behavior.

## Local Contracts

- Faithful passthrough: forward request/response shape unchanged except metering + sanitize.
- Paid inference/media routes go through the shared rate limiter (`ratelimit.py`) keyed by
  API key. Not every endpoint is limited — `models`, `stats`, `health`/`metrics`, and progress
  polling are unlimited by design; wire the limiter on new work-submitting routes explicitly.
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
- Validator endpoints are evidence-only until the validator role, rewards,
  and dispute process are wired. Do not let `failed` attestations affect worker
  strikes/slashing from this router.
- Assignment-bound evidence must require a Grid-issued `assignment_id`,
  `grid_nonce`, and matching hard-targeted probe evidence hash before it is
  marked authoritative. Preview evidence may be stored, but must stay labeled
  as preview.
- Validator scorecards must aggregate evidence only. Do not expose raw payloads,
  nonces, signatures, account IDs, or validator identities from scorecard routes.
- Targeted validator probes must be hard-targeted to the assigned worker and
  must not bill users, pay den, write worker ledger rows, or strike workers.
- Generation routes accept Core-issued `X-Grid-User-Token` delegation from a
  service key. Legacy `X-Grid-User-Assertion` is app-local only and cannot claim
  Google or wallet identity. Account management needs a recent Core-verified
  Google/SIWE proof.

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
