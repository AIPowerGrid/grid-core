# grid_api/routers - HTTP + WebSocket endpoints

## Purpose

The grid's external surface. OpenAI/Anthropic-compatible inference, media gen, worker
transport, accounts, stats, health/metrics.

## Ownership

- `openai.py` - `POST /v1/chat/completions`,
  `POST /v1/x402/chat/completions`, `GET /v1/models`,
  `GET /v1/models/{model_id}`. Sanitizes messages pre-dispatch, detects
  chat-routed media models, reserves text credits in live mode, and streams or
  collects worker output.
  The x402 route is a separate default-off, accountless Base-USDC lane. It is
  non-streaming and text-only until a stream-aware settlement adapter exists.
- `anthropic.py` - `POST /v1/messages` raw Anthropic Messages passthrough.
- `responses.py` - `POST /v1/responses` raw OpenAI Responses passthrough.
- `_passthrough.py` - shared raw passthrough submit/stream/collect and deep
  secret sanitization helpers.
- `images.py` - `POST /v1/images/generations` native image jobs.
- `videos.py` - `POST /v1/videos/generations` native video jobs.
- `audio.py` - `POST /v1/audio/generations` governed local ACE-Step jobs.
- `worker_enrollment.py` - dark device-style manager pairing. Public create,
  intent, poll, and ACK endpoints are capability-bound by a high-entropy
  enrollment ID plus poll secret; prepare/approve require a recent scoped user
  token and a payout-wallet signature.
- `worker_ws.py` - `/v1/workers/ws`: registration + dispatch + health/eviction + streaming.
  **God-file (~1.1K LOC); split target = registration / dispatch / health / stream.** Highest
  bug history (eviction cascade, idle-redelivery) - change carefully, add tests.
  Assignment-bound image/video probes branch before ordinary media settlement: they
  strip all `_validator_*` metadata, freeze the uploaded object through Core,
  acknowledge with `den: 0`, and never touch customer or worker economics.
- `accounts.py` - native Google/strict EIP-4361 SIWE auth, bounded service exchange, and
  default-off legacy dashboard/internal session creation,
  account profile (incl. resolved `payout{asset, aipg_bps, active, live_asset}`),
  payout wallet + `POST /v1/account/payout-preference` (both SESSION-gated),
  worker listing, API-key issue/revoke, `GET /v1/account/credits` (canonical
  `account_id` plus promotional/free/paid pockets; `total_spendable_*` = what
  can pay NOW vs `total_preview_*`; `free.active` tracks
  GRID_FREE_SPENDABLE_LIVE), `POST /v1/account/credits/quote` (the same balance
  truth plus a non-mutating, reservation-equivalent model/modality estimate and
  expected pocket split), `GET /v1/account/jobs`
  (operator trust view: my workers' jobs + den + result_hash + signed flag,
  scoped to the payout wallet), immutable deposit history/config, and deposit
  claims (USDC launch rail, bounded expiring-price AIPG, actual-USDC
  swap-receipt ETH, and operator-only buffered ETH).
  `POST /v1/accounts/session` is the retired internal-token bridge. It
  resolves on exactly one authoritative identity (`oauth_sub` first, then
  wallet, then verified email only when it is the sole identity); supplemental
  or unverified email must never join accounts.
  Native service/app exchange lives at `/v1/auth/service/exchange`; Google ID
  tokens are verified at `/v1/auth/google/exchange`, which also returns the
  canonical account's primary verified wallet when one is linked; partner
  wallet proof uses
  `/v1/auth/wallet/challenge` plus `/v1/auth/wallet/exchange`, bound to the
  service, its exact `siwe_domains`, the app subject, wallet, URI, Base chain,
  expiry, and one-use nonce. `/v1/auth/service/bind` binds an app subject after
  recent Google/SIWE proof. Bind accepts either a direct step-up token or a
  step-up token audience-bound to that same service. `/v1/accounts/bridges` bootstraps a bounded service
  client only when separately enabled.
  Wallet login uses `/v1/accounts/wallet/challenge`; the signed message binds
  wallet, allowlisted frontend domain/URI, Base chain id, issue/expiry time, and
  a single-use nonce. `/wallet/nonce` remains for authenticated wallet-link
  proofs, not for minting a login session.
- `stats.py` - `GET /v1/workers`, progress polling, recipe-aware model status
  (raw worker checkpoints plus executable recipe-backed public model names), usage totals,
  model stats, wallet earnings, `GET /v1/payouts/public` (aggregate payout
  transparency), `GET /v1/jobs/recent` (PUBLIC redacted job feed: model, worker
  handle, timing, den, prompt/result hashes + signed flag — NEVER content,
  NEVER customer wallet/account), and `GET /v1/status/network` (public,
  privacy-safe worker/model capacity, validator aggregates, charging mode,
  payout totals, current component incidents, and decentralization advisories).
- `validator.py` - validator assignment-bound evidence surface:
  `GET /v1/validator/capabilities`, signed linked-wallet registration/status/
  heartbeat, signed self-suspension and linked replacement-wallet rotation,
  `GET /v1/validator/assignments`,
  `POST /v1/validator/probe/{assignment_id}`,
  `POST /v1/validator/attest`, `GET /v1/validator/workers`,
  `GET /v1/validator/scorecards`, and
  `GET /v1/validator/assignments/health`. Health separates probe,
  accepted-evidence, worker-pass, quorum, finalization, and aggregate validator
  liveness stages. Shared 3-of-5 quorum remains preview-only with no
  routing/reward/slash authority. The image-fidelity assignment lane is
  default-off and fail-closed on governed recipe/model digest, bond/reference
  policy, and validator capability. The separately gated video-contract lane is
  default-off, requires an explicit governed timing recipe, and verifies one
  candidate MP4 without claiming fidelity.
  Health also exposes privacy-preserving network aggregates over a bounded
  `since_hours` window; never relabel registered validators as independently
  operated validators.
- `styles.py` - `GET /v1/styles` for curated creative presets.
- `health.py` - `GET /health`, including the immutable full release commit when
  the runtime can prove it from `GRID_BUILD_COMMIT` or a detached checkout.
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
- x402 requests must use the external reservation path and return the final
  grid-counted micro-USD amount through the SDK settlement override. Never let
  them draw daily free, promotional, or purchased account credit.
- `worker_ws.py` must not trust worker-reported counts for rewards or customer
  billing without a server-side cap or verification path.
- Media completion must report exactly one unique canonical digest per
  presigned output slot, and every expected R2 object must pass existence,
  content-type, and size validation before payout or demand settlement.
- Retried failures from one job count as at most one health strike per worker;
  worker eviction requires independent failed jobs, not repeated poison-job
  deliveries.
- Core rejects retired model identities during the worker handshake. Worker-side
  filtering is defense in depth, not the network authority for retirement.
- Media routes must pass `user.get("account_id")` to `services.media`; quota IDs
  like `v2:<uuid>` are not credit ledger account IDs.
- Successful media envelopes carry `grid.job_id`; preserve it through first-party
  brokers so users can identify the corresponding completion and charge record.
- Worker affinity (`worker` request field) is ownership-gated before queueing.
- Public stats/health/metrics are unauthenticated by design; keep sensitive
  account/ledger details behind account auth.
- Validator endpoints are evidence-only until the validator role, rewards,
  and dispute process are wired. Do not let `failed` attestations affect worker
  strikes/slashing from this router.
- Validator work routes require an active `grid_validators` registration bound
  to the API-key account's linked wallet. Dedicated validator keys have exactly
  `validator.assignments`, `validator.probe`, `validator.attest`, and
  `validator.read`; never broaden them to inference or account-management
  authority.
- Validator suspension requires a fresh signature from the currently registered
  wallet. Rotation preserves the stable validator ID, requires the same
  canonical account to link and sign with a different replacement wallet, and
  cannot revive a maintainer-revoked registration. Ordinary signed registration
  is the explicit resume path for a self-suspended validator.
- Assignment-bound evidence must require a Grid-issued `assignment_id`,
  `grid_nonce`, and matching hard-targeted probe evidence hash before it is
  marked authoritative. Preview evidence may be stored, but must stay labeled
  as preview.
- Sealed assignment polling is compatibility-gated: when enabled, list responses
  expose only opaque lifecycle/capability metadata and a SHA-256 seal. Target,
  model, nonce, policy, and challenge may appear only in the terminal probe
  disclosure, whose seal the node verifies before signing. Keep legacy full
  polling until all participating nodes support the sealed form.
- A completed targeted probe is recoverable only by the assignment's canonical
  account and registered validator, only until that validator submits its
  authoritative vote, and only through the bounded result envelope committed
  by Core. A recovery request must return `replayed: true`; it must not dispatch
  another worker job, consume another attempt, or expose another validator's
  result.
- Validator scorecards must aggregate evidence only. Do not expose raw payloads,
  nonces, signatures, account IDs, or validator identities from scorecard routes.
- Public-template validator probes are adversarially reproducible by parsers and
  probe-aware model switching. Keep the hostile-worker contract test in CI and
  never mark these generated probes as quality-eligible.
  They must expose the evidence dimension and whether it is quality-eligible;
  current generated canaries must return `quality_eligible=false` and no quality
  score. They must also distinguish objective assignment votes cross-checked
  against Core's independently computed verdict from validator-only opinions.
  A disagreement remains evidence, not a hidden Core-verified result.
- Targeted validator probes must be hard-targeted to the assigned worker and
  must not bill users, reward validators, grant evidence authority, or strike
  workers. Evidence-only assignments pay no den. A text assignment explicitly
  snapshotted as `audit_budget` may pay its target worker only through the
  reviewed-wallet, durable-budget, atomic ledger path in `validator_audits.py`.
  Worker-visible job IDs and payloads must not reveal validator markers,
  assignment/group IDs, or Grid nonces; evidence binding stays inside Core.
  The evidence-only terminal `den: 0` acknowledgment is a retrospective probe
  fingerprint. Paid text audits use an ordinary calculated den ACK after the
  budget/ledger commit; recognizable prompt templates remain a fingerprint, so
  the API must not claim full probe indistinguishability.
- A media witness is authoritative transport only after Core freezes the upload
  under a key the worker cannot write and hashes the frozen bytes. Worker-reported
  digests, mutable upload URLs, and self-declared model names are never evidence.
- Media worker execution is group-owned, not validator-owned: image fidelity
  runs one candidate plus two references once; video contract runs one candidate
  once. Every quorum member independently scores the same committed frozen
  witness set.
- Missing validator registration/assignment/probe support must fail closed.
  Ordinary chat inference and worker inventory are never fallback targeting
  paths.
- Generation routes accept Core-issued `X-Grid-User-Token` delegation from a
  service key. Legacy `X-Grid-User-Assertion` is app-local only and cannot claim
  Google or wallet identity. Account management needs a recent Core-verified
  Google/SIWE proof.
- A service may submit an `app_subject` during Google or wallet exchange only
  when it derives that value from its authenticated server session. Never trust
  a browser-supplied account/user id: proof exchange may merge value-bearing
  accounts.

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
- Worker pairing/auth changes: include
  `grid_api/routers/tests/test_worker_enrollment_contract.py` and the service
  lifecycle tests before the full suite.
- `pytest grid_api/services/tests/test_credits_billing.py` when changing any
  route that reserves, refunds, or reconciles credits.

## Child DOX Index

- `tests/` - router-level pytest coverage.
