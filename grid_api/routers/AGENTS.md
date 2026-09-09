# grid_api/routers - HTTP + WebSocket endpoints

## Purpose

The grid's external surface. OpenAI/Anthropic-compatible inference, media gen, worker
transport, accounts, stats, health/metrics.

## Ownership

- `openai.py` - `POST /v1/chat/completions`,
  `POST /v1/x402/chat/completions`, `GET /v1/models`,
  `GET /v1/models/{model_id}`. Sanitizes messages pre-dispatch, detects
  chat-routed media models, reserves text credits in live mode, and streams or
  collects worker output. The request model normalizes OpenAI's current
  `max_completion_tokens` field to the Grid's metered `max_tokens` worker cap;
  conflicting dual fields are rejected.
  The x402 route is a separate default-off, accountless Base-USDC lane. It is
  non-streaming and text-only until a stream-aware settlement adapter exists.
- `anthropic.py` - `POST /v1/messages` raw Anthropic Messages passthrough.
- `responses.py` - `POST /v1/responses` raw OpenAI Responses passthrough.
- `pricing.py` - public, unauthenticated `GET /v1/pricing`; exposes the exact
  versioned USD price book plus only fresh, source-linked same-model comparison
  workloads. Expired comparison evidence is omitted rather than repeated.
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
  Durable media recovery retains Core's dispatched recipe root for image and
  video as well as audio. Never replace it with the worker's reported root.
  Audio still requires its existing matching-root echo; image/video retention
  is routing evidence only and does not add an execution-attestation claim.
  **God-file (~1.1K LOC); split target = registration / dispatch / health / stream.** Highest
  bug history (eviction cascade, idle-redelivery) - change carefully, add tests.
  Assignment-bound image/video probes branch before ordinary media settlement: they
  strip all `_validator_*` metadata, freeze the uploaded object through Core,
  acknowledge with `den: 0`, and never touch customer or worker economics.
  Assignment-bound text probes branch before ordinary raw passthrough as well.
  The internal Responses qualification adapter captures bounded native
  output-text probabilities through the dedicated no-den collector; it never
  invokes the paid passthrough handler. Missing or partial observations do not
  become failed-worker votes. Time/size overruns cancel and close the socket so
  late frames cannot contaminate a subsequent job. No public assignment policy
  currently selects this adapter, and its observations cannot feed the existing
  chat first-token score without separate context-alignment calibration.
  The separate compensated-audit hold, when present for an ordinary job UUID,
  settles through the exact text/media/passthrough paid terminal: ordinary frame,
  nonzero den acknowledgement, and no worker-visible audit marker. No scheduler
  creates those jobs yet.
  After an ordinary compatible job is actually sent, worker transport calls the
  neutral `route_events` emitter without awaiting it. Terminal attempt outcomes
  use the same non-awaited handoff. The emitter cannot fail, delay, retry, ack,
  settle, pay, or change a job; only the isolated background collector may read
  those private outbox events into validator shadow records.
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
  `GET /v1/workers/self` and `POST /v1/workers/self/canary` accept only a
  manager-issued `worker.connect`
  credential. The status route returns the exact bound rig's online/jobs/den
  state plus a redacted account-level payout lifecycle summary; the canary runs
  one randomized, hard-targeted, economically inert text or governed media
  connectivity check selected from the rig's advertised capabilities. It accepts
  no caller-selected prompt, model, or worker and makes no model-identity,
  intelligence, or quality claim. These routes never grant account
  reads, enumerate sibling workers, or expose payout addresses, amounts,
  balances, or hashes.
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
  payout totals, current component incidents, decentralization advisories, and
  privacy-safe manager-enrollment completion, registration, and rolling
  seven-day worker-retention aggregates. A setup completion is counted only
  after the manager ACK permanently activates its bound `worker.connect`
  credential. Distinct rig labels are counted even if that credential is later
  rotated or revoked; abandoned temporary credentials do not count. Download
  counts remain owned by GitHub Releases.
- `validator.py` - validator assignment-bound evidence surface:
  `GET /v1/validator/capabilities`, signed linked-wallet registration/status/
  heartbeat, signed self-suspension and linked replacement-wallet rotation,
  `GET /v1/validator/assignments`,
  `POST /v1/validator/probe/{assignment_id}`,
  `POST /v1/validator/attest`, `GET /v1/validator/workers`,
  `GET /v1/validator/scorecards`, and
  `GET /v1/validator/assignments/health`. The unauthenticated
  `GET /v1/validator/public/{validator_id}` exposes only a shareable validator
  ID's rounded heartbeat, version, aggregate activity, qualification progress,
  and actionable status. Mature candidates are prompted to request maintainer
  review without receiving independence or economic authority; operational
  repair/upgrade actions retain precedence. It never returns account, wallet, signature, operator
  group, review reference, assignment, or evidence data. Health separates probe,
  accepted-evidence, worker-pass, quorum, finalization, and aggregate validator
  liveness stages. Shared 3-of-5 quorum remains preview-only with no
  routing/reward/slash authority. The image-fidelity assignment lane is
  default-off and fail-closed on governed recipe/model digest, bond/reference
  policy, and validator capability. The separately gated video-fidelity lane is
  default-off and requires a governed deterministic timing recipe, one
  candidate MP4, and two independently controlled bonded references.
  The `text-fidelity` assignment selector is a separate default-off lane: Core
  hard-targets one candidate plus configured same-model references and returns
  only a bounded, committed first-token distribution witness set. It never
  turns missing logprobs or a lone-reference mismatch into worker-failure
  evidence.
  Health also exposes privacy-preserving network aggregates over a bounded
  `since_hours` window; never relabel registered validators as independently
  operated validators.
- `validator_pairing.py` - default-off, two-party account association for an
  already registered node. `/v1/validator/account-pairings*`,
  `/v1/validator/account-pairing`, and `/v1/validator/account-link*` use the
  existing validator key; `/v1/account/validator-pairings*` and
  `/v1/account/validators*` require a Core user token. Approval/removal require
  fresh Google/SIWE proof. Browser approval alone does not link: the current
  node must sign the exact expiring account-bound payload. No account merges,
  new credentials, wallet changes, control-group changes, or economic effects.
- `validator_compensation.py` - eight default-off private routes under
  `/v1/validator/compensation*` and
  `/v1/account/validator-compensation/requests*`: node status/start/read/confirm/
  cancel and human read/prepare/approve. Node reads/writes require
  `validator.read`/`validator.attest`; human routes require Core user tokens,
  `account.read`/`account.manage`, and recent step-up for writes. Bodies are
  bounded before parsing; responses are no-store with fixed validation/storage
  errors. Both signatures collect review evidence only, never recipient approval
  or payment. See `docs/architecture/VALIDATOR_PAYOUT_CONSENT.md`; node-app and
  Console screens remain separate delivery gates.
- `styles.py` - `GET /v1/styles` for curated creative presets.
- `health.py` - `GET /health`, including the immutable full release commit when
  the runtime can prove it from `GRID_BUILD_COMMIT` or a detached checkout.
- `metrics.py` - `GET /metrics` Prometheus exposition.
- `oauth.py` - default-off OAuth discovery, bounded public-client registration,
  S256 authorization/token exchange, Console-only consent inspection/decision,
  and service-only token introspection for the future remote MCP resource.
- `tests/` - router-level tests, including billing/settlement behavior.

## Local Contracts

- `GET /v1/account/ownership` is private, read-only, `account.read` gated,
  rate-limited and `no-store`. Service keys must delegate a user. It accepts no
  target account and returns only the authenticated canonical account plus
  proved retired aliases (at most 128 family members); database/graph errors
  fail closed. This is not permission to merge accounts or rewrite ledgers.
  Frontends must not accept a browser-supplied alias as equivalent evidence.

- `media_results.py` owns `GET /v1/media/results?job_id=...` or `?client_ref=...`
  (exactly one). It requires `inference.submit` with the same real service/user
  delegation as generation, returns only the canonical account family's
  reservation/result, and is rate-limited to 60 reads/minute. Responses are
  no-store. Missing/foreign jobs return indistinguishable 404s, conflicting
  refs 409, and unavailable/corrupt storage 503 without upstream details.
  `pending`, `completed`, and `closed_without_result` are recovery states;
  absent output is not proof that work was cancelled or refunded. The endpoint
  neither submits jobs nor changes credit, reward, or free-allowance state.

- Faithful passthrough: forward request/response shape unchanged except metering + sanitize.
- Paid inference/media routes go through the shared rate limiter (`ratelimit.py`) keyed by
  API key. Not every endpoint is limited — `models`, `stats`, `health`/`metrics`, and progress
  polling are unlimited by design; wire the limiter on new work-submitting routes explicitly.
- Demand billing must be applied uniformly across all paid inference entry
  points before live charging. Do not add a new work-submitting route without
  reserve/reconcile or an explicit no-charge policy.
- Chat `auto*` routing excludes models without an applicable text price when
  the request is chargeable (always for x402), before ranking or quota use.
  No eligible curated model returns 503 before dispatch. Explicit model names
  remain unchanged and retain the normal authorization/reservation checks.
- x402 requests must use the external reservation path and return the final
  grid-counted micro-USD amount through the SDK settlement override. Never let
  them draw daily free, promotional, or purchased account credit.
- `worker_ws.py` must not trust worker-reported counts for rewards or customer
  billing without a server-side cap or verification path.
- Chat settlement commits all witnessed output channels through
  `services/chat_output.py`, including tool-only and reasoning-only replies.
  Grid token counts include assembled function names/arguments, not call IDs
  or chunk counts; the requested maximum remains the billing cap. Terminal
  self-reports cannot replace an observed stream. HTTP collectors share the counter for dry-run/display
  fallback but never settle money. Historical result hashes are not rewritten.
- Worker-reported text logprobs are untrusted evidence. Normalize and bound the
  first distribution before it reaches Redis; never retain an arbitrary nested
  backend payload or treat it as cryptographic model identity.
  Responses qualification instead preserves bounded native per-delta records,
  missing-probability gaps, sequence/item indices, and visible-prefix hashes.
  These hashes are not full model-context commitments: hidden reasoning,
  tokenizer and chat-template equivalence remain unverified.
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
  Additive sampling/freshness metadata distinguishes vote counts from retained
  probe groups and completed-probe age from receipt age. Do not turn null
  confidence intervals or independent sample counts into zero or a green
  confidence indicator. `/v1/validator/scorecards` remains active-validator and
  `validator.read` gated. `/v1/account/validator-scorecards` exposes the same
  redacted network aggregates to authenticated v2 `account.read` credentials,
  including Google-only and service-refreshed user sessions without a node.
  This grants no private assignment health, registration, probe, work or
  attestation authority; those existing node routes retain their gates.
- Public-template validator probes are adversarially reproducible by parsers and
  probe-aware model switching. Keep the hostile-worker contract test in CI and
  never mark these generated probes as quality-eligible.
  They must expose the evidence dimension and whether it is quality-eligible;
  current generated canaries must return `quality_eligible=false` and no quality
  score. They must also distinguish objective assignment votes cross-checked
  against Core's independently computed verdict from validator-only opinions.
  A disagreement remains evidence, not a hidden Core-verified result.
- Targeted validator probes must be hard-targeted to the assigned worker and
  must not bill users, pay den, write worker ledger rows, or strike workers.
  Worker-visible job IDs and payloads must not reveal validator markers,
  assignment/group IDs, or Grid nonces; evidence binding stays inside Core.
  The current terminal `den: 0` acknowledgment is a retrospective probe
  fingerprint. It is acceptable only while evidence has no economic authority;
  the separate bounded compensated-audit terminal now has an ordinary worker-
  payment shape, but remains unreachable without a scheduler. Existing probes
  must not be silently converted into that rail. Routing/reward/slash activation
  still requires the scheduler, held-out traffic-classifier gate, and independent
  operator evidence.
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
- Public generation admission is independent of identity and charging mode.
  Chat text checks `openai-chat`; passthrough reserves check their fixed
  `openai-responses` / `anthropic` format. All media, including chat's media
  abstraction and Director timeline payloads, passes the shared service gate.
  Disabled paths return 503 before a new hold/dispatch; terminal handlers keep
  draining already admitted work. Availability/price listings alone do not
  establish that an operationally gated path is enabled.
- OAuth access tokens have no `service_id`; while the OAuth gate is live Core
  accepts them only for the configured exact resource audience. The remote MCP
  backend key may carry only `oauth.introspect`; never reuse a frontend bridge
  key or grant it identity exchange/direct inference authority. Production
  Nginx must keep the introspection route external-dark; the co-located MCP
  process reaches it through loopback Uvicorn only.
- A service may submit an `app_subject` during Google or wallet exchange only
  when it derives that value from its authenticated server session. Never trust
  a browser-supplied account/user id: proof exchange may merge value-bearing
  accounts.

Validator account pairing is optional visibility metadata, not an auth or
recovery identity. Never use it to authenticate validator work or change
payout ownership. Keep `VALIDATOR_PAIRING_ENABLED=0` until Console, local-app
consent, and platform end-to-end qualification ship together. See
`docs/architecture/VALIDATOR_ACCOUNT_PAIRING.md` for the full route contract.
The separate expiring `VALIDATOR_PAIRING_CANARY_ACCOUNTS` pilot permits only
configured canonical node/human accounts while public availability stays false.
It is an extra service-layer restriction, never an authentication bypass. Empty,
expired or out-of-scope pilots return 503 without revealing membership.

## Work Guidance

- Validator qualification views expose additive `coverage_basis`,
  `lifetime_sample_coverage`, `recovery_window_seconds`,
  `recovery_observed_seconds` and `recovery_window_ready`. Never return the
  private raw heartbeat ring. A recent-window recovery is not an operator
  independence grant; retain the separate review/freshness/version gates.

- New endpoint -> add a contract test; wire auth + rate limit; route media via `services/media.py`,
  text via `services/job_queue` + `token_stream`.
- Worker self-canaries are setup evidence only. They must hard-target the exact
  manager-bound online text or media worker, carry randomized Core-only binding, remain
  rate-limited, and terminate through the dedicated `den: 0` path. Malformed
  canary metadata must be acknowledged as an error before ordinary settlement.
  Media canaries use only governed source-free recipes, verify the expected
  upload and required managed-worker receipt, bound execution below the Core
  observation timeout, and best-effort delete their temporary output.
- OAuth registration and token requests must stay explicitly body-bounded before
  parsing. Never redirect an error to an unvalidated URI, expose a plaintext
  request/code capability, accept a confidential-client secret, or weaken S256
  PKCE for a native/agent client.
- Prefer small helpers over expanding `worker_ws.py`. If a change affects worker
  registration, job dispatch, streaming, media, or health separately, consider a
  local extraction with tests.
- Preserve OpenAI/Anthropic error shapes where SDK compatibility depends on them.
- Keep request-size checks before sanitizer/tokenization for CPU and memory safety.

## Verification

- `pytest grid_api/routers/`.
- `tests/test_worker_billing_failures_postgres.py` uses disposable
  `CREDITS_TEST_DB_URL` and a unique schema per test. It runs the actual worker
  registration/dispatch loop and credit transactions for chat, both native
  passthrough formats, image, video and audio: errors refund once; disconnects
  retain holds only while requeued; give-up refunds; an interrupted refund
  transaction remains held until the sweeper recovers it; late success cannot
  mint a reward afterward. A child process is killed after the refund's SQL
  status update but before its credit write; rollback and sweeper recovery are
  checked from another connection. Auth, worker I/O, Redis queue/registry, client
  event delivery and R2 are simulated. This is not live-worker qualification,
  elapsed receive-timeout verification or a full Uvicorn/Redis crash test. Required
  PR/main CI supplies PostgreSQL 16 to the full suite.
- `tests/test_validator_evidence_postgres.py` requires disposable
  `VALIDATORS_TEST_DB_URL`: signed binding/identity corruption, expired or
  unfinished probes, concurrent duplicate/conflicting votes, and disagreement.
  It synthesizes completed probe evidence and tests the storage service, not
  HTTP authentication, model fidelity, or compensation. CI supplies PostgreSQL
  16; local PostgreSQL 14 results are supplementary, not release qualification.
- `tests/test_validator_scorecards_postgres.py` uses the same disposable PG
  fixture and signed synthetic evidence to prove shared-group counts, bounded
  receipt windows, actual completed-probe freshness, null/future timestamps,
  real foreign-key pruning and read-only aggregate access. Three registrations
  remain one probe group with unknown independence; these tests do not prove
  inference, production HTTP authorization or a deployed scorecard.
- Worker pairing/auth changes: include
  `grid_api/routers/tests/test_worker_enrollment_contract.py` and the service
  lifecycle tests before the full suite.
- `pytest grid_api/services/tests/test_credits_billing.py` when changing any
  route that reserves, refunds, or reconciles credits.

## Child DOX Index

- `tests/` - router-level pytest coverage.
