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
  Authenticated `GET /v1/models/{model_id}/recipe` retains legacy recipe shape;
  governed video entries add `video_contract` and separately expanded
  `source_requirements` node/file inventories. Preserve the stored/base graph.
  Inventory reads never enable admission or prove execution/fidelity.
  The x402 route is a separate default-off, accountless Base-USDC lane. It is
  non-streaming and text-only until a stream-aware settlement adapter exists.
- `anthropic.py` - `POST /v1/messages` raw Anthropic Messages passthrough.
- `responses.py` - `POST /v1/responses` raw OpenAI Responses passthrough.
- `pricing.py` - public, unauthenticated `GET /v1/pricing`; exposes the exact
  versioned USD price book plus only fresh, source-linked same-model comparison
  workloads. Expired comparison evidence is omitted rather than repeated.
- Text model discovery and status include additive `pricing` with USD input/
  output rates per million tokens, source (`model` or `default`) and book
  version. Unknown text models use the published standard tariff, not free
  inference; explicit media/zero-rate entries and unresolved auto do not.
  This is customer pricing only, never model verification or reward approval.
- `_passthrough.py` - shared raw passthrough submit/stream/collect and deep
  secret sanitization helpers.
- `images.py` - `POST /v1/images/generations` native image jobs.
- `videos.py` - `POST /v1/videos/generations` native video jobs.
  Contract-backed LTX-2.5 candidates support exact 8n+1 frames and at most
  sixteen ordered image pins, starting at frame zero. All images validate before
  quota/upload; multi-pin jobs additionally require `video-timeline` admission
  and the `recipe-image-bindings-v1` worker feature. Unsupported settings reject before quota/upload; the
  Distilled recipes are promoted for a bounded pilot; deployment/canary evidence
  is still required. Dev/Ingredients remain non-serving. See `LTX_2_5_VIDEO.md`.
  Distilled candidates additionally accept explicit inline `reference_audio`
  (complete exact-duration 48 kHz stereo PCM16 WAV) and `reference_video`
  (fully decoded exact-frame 24 fps H.264 MP4 at the explicit requested size).
  Motion references require a separate audio input; neither role is an opening
  image. Both source modes are default-dark and require compatible workers.
  All typed LTX inputs, including optional image pins, require the separate
  private-source bucket before quota/uploads; no output-bucket fallback.
  `_video_body.py` bounds both native video JSON routes to 64 MiB + 64 KiB
  before parsing and redacts input-bearing validation errors. The combined
  encoded source budget is 64 MiB, with stricter per-kind/image budgets.
  Source byte hashes join durable normalized request identity; replay never
  reuploads, trims or regenerates a source. Input roles/bytes must match the
  governed graph before reserve. This is not character identity or lip accuracy.
  The separate `LTX-2.5 Dev BF16 Ingredients` candidate accepts `reference_image`
  as appearance-only PNG conditioning with source audio and optional motion
  video, never `image`/`keyframes`. Its fixed 121-frame/768x448/40-step profile
  cannot widen ordinary Dev. Finite `reference_strength`, optional
  `reference_attention_strength` and video-only `motion_attention_strength`
  join normalized intent; absent new fields preserve
  existing request hashes. `appearance-to-video` is default-off and requires
  `recipe-appearance-reference-v1` on the same worker as every other feature.
  The recipe stays non-serving/unpriced; compiler tests do not qualify output.
  Reference attention has no advertised default: omission keeps the original
  appearance guide, while explicit (0, 1] selects Core's Advanced node only
  there. The same node is required in both authenticated source inventories.
- `video_jobs.py` - candidate `POST /v1/videos/jobs` and owner-only
  `GET /v1/videos/jobs/{job_id}`, plus `GET /v1/videos/jobs` with the
  `Idempotency-Key` header for lost initial replies. GET never admits, uploads,
  dispatches or moves money; unknown/foreign keys return indistinguishable 404s
  and merged duplicate keys fail 409. Key text is not a URL selector or bearer
  capability. The private response adds the stored normalized `request_hash`
  so a client can bind recovery to its own intent. New admission defaults off.
  A separate default `pilot` mode admits only a configured private account
  cohort before its <=24-hour expiry. Fresh blocked keys reject before source
  decoding/staging; replay checks the original intent and does not charge or
  upload again. Recheck admission after staging and capture the earlier deadline
  for the credit transaction. Contract-backed legacy waiting/chat video cannot
  bypass pilot mode; public `open` is an explicit, separately reviewed choice.
  Public async admission defaults to new Distilled BF16 jobs only. The separate
  default-off `MEDIA_ASYNC_NVFP4_ENABLED` opt-in permits qualified NVFP4 text/
  opening-image jobs, never uploaded audio/video. Its existing337-frame limit,
  positive quote, one hold, model binding and recovery remain unchanged. Other model
  identities reject before private source decoding, while existing pilot
  intent replay and owner-only GET recovery remain available.
  Public-mode reference audio/video/image also passes the separate source
  admission gate at all three fresh checks. Default off rejects before request
  hashing/decoding/quota/uploads; an independent source pilot freezes its own
  earlier deadline for the locked hold transaction. Text/opening-image work
  remains public while this source gate closes. Existing source receipts stay
  readable and replayable without another hold or upload.
  Scoped `inference.submit` authentication, a UUIDv4 `Idempotency-Key`, a positive
  `max_cost_micro_usd` and a live credit hold are required. The typed body is
  bounded before JSON parsing and input-bearing validation errors are redacted.
  Replay/recovery remain available after admission rollback; changed intent
  returns 409. One committed hold/journal accepts the request before the outbox
  queues it. Returns 202 for pending/queued/uncertain execution and 200 for a
  committed terminal, with `Location`, private `no-store`, and bounded polling.
  Workers must themselves support every required feature, including
  `async-video-resume-v1`; availability is not a union of unrelated workers'
  features. Released/default workers do not advertise that new capability;
  candidate opt-in startup requires matching authenticated recipe inventories.
  Input validation is separate from staging. Async ingress compiles its private
  immutable graph/seed/source snapshot before quota or any input upload, then
  binds locations without re-resolving the recipe. Invalid compilation consumes
  neither quota nor credits and uploads nothing. This in-memory execution plan
  is not an immutable payment quote or durable x402 authority. The waiting
  video route keeps its existing credit lifecycle and response shape; plain
  opening images now also validate before quota, not during upload.
  This is credit-only, not x402, cancellation or worker-restart qualification.
- `audio.py` - `POST /v1/audio/generations` governed local ACE-Step jobs.
- `worker_enrollment.py` - dark device-style manager pairing. Public create,
  intent, poll, and ACK endpoints are capability-bound by a high-entropy
  enrollment ID plus poll secret; prepare/approve require a recent scoped user
  token and a payout-wallet signature.
- `worker_ws.py` - `/v1/workers/ws`: registration + dispatch + health/eviction + streaming.
  Internally marked async video jobs require a committed `media_jobs` execution
  claim before presigning or WS dispatch. Duplicate/closed jobs acknowledge only
  the queue, never another worker payment or client DONE. Claim errors do not
  dispatch or acknowledge the queue. Only the default-off scoped async ingress
  creates these jobs; the fence remains required during admission rollback.
  Mismatch/feature/format exhaustion, disconnect give-up and prefetch cleanup
  use the unstarted-only release guard for marked jobs. Genuine render failures
  must hold the current Core execution lease; a duplicate queue delivery cannot refund
  an already-running worker's hold.
  Model/feature/format mismatch first seeks one fully compatible live peer and
  transfers the original pending delivery, even if that peer is busy. Core's
  registry adds its own inbox-protocol marker; worker claims cannot opt an older
  coordinator into receiving it. Exact fresh snapshots and consumer ownership
  fence the handoff. No-peer search keeps the existing bounce cap; busy-peer
  wait has a separate bounded deadline, checked again at dispatch after local
  prefetch. Superseded pending owners cannot dispatch or refund that delivery.
  Expiry sends no backend job/DONE or
  worker payout and uses the same unstarted-only async release guard. Hard
  targeting and soft-affinity policies are not relaxed by this candidate.
  Candidate original-worker reconnect uses SQL, not Redis/model routing: the
  exact worker UUID, checkpoint and features must still match. After the prior
  60-second lease expires, the same job/graph/seed can receive `resume:true` and
  fresh, attempt-isolated upload slots. Renewal stops at the original 30-minute
  budget. Success and failure validate the private lease in the money transaction;
  superseded sessions publish no DONE/error/ACK, DEN or success metrics. Frame
  job IDs must match. Core never sends its private lease token to the worker.
  Before a candidate async video's paid terminal, Core source-conditionally
  copies its bounded upload to a unique key the worker cannot write and hashes
  the frozen bytes. The committed result uses that copy. Invalid bytes refund
  only under the current lease; storage uncertainty keeps the hold and sends
  no success/ACK. This is byte integrity, not model fidelity or codec quality;
  legacy media retains its existing HEAD/self-reported-hash path.
  The worker must refuse a missing local journal instead of starting a render.
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
  Video quotes validate only shared exact timing, without requiring private
  conditioning media. Ingredients quotes retain the fixed 121-frame profile
  and unpriced status until an operator-reviewed rate exists; a quote never
  uploads, reserves, dispatches or enables its default-dark admission.
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
- `worker_media.py` - `POST /v1/workers/self/media-jobs`, a private read-only
  cache-reconciliation authority for the exact rig bound to a dedicated worker
  credential. It does not grant account access, dispatch or financial writes.
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
  `video-async-credit-jobs-v1` advertises the async credit/intent/recovery
  contract, not enabled admission, available workers or financial qualification.
- `metrics.py` - `GET /metrics` Prometheus exposition.
- `oauth.py` - default-off OAuth discovery, bounded public-client registration,
  S256 authorization/token exchange, Console-only consent inspection/decision,
  and service-only token introspection for the future remote MCP resource.
- `tests/` - router-level tests, including billing/settlement behavior.

## Local Contracts

- Worker media-status accepts 1..32 unique canonical job UUIDs in a body bounded
  to 2048 bytes before parsing. Require `worker.connect`, v2 `key_kind=worker`
  and the exact `worker:{name}` label; resolve that registered worker only
  within its proved account family. Do not compare the paying customer's
  `media_jobs.account_id` to the worker owner. Only jobs whose original
  `execution_worker_id` is this worker are visible. A committed `settled`
  reservation is `closed`; a current held execution is `held`; missing,
  foreign, expired, legacy or uncertain jobs are indistinguishable `unknown`.
  Never infer closure from elapsed time. Return only worker/job IDs and these
  states, use no-store and 120 reads/minute, and continue serving recovery with
  new async admission disabled. DB ambiguity fails 503 without private details.

- `GET /v1/account` remains `account.read` gated and returns `no-store`.
  Raw linked-identity display hints and sibling API-key metadata require both
  `account.manage` and an interactive native/delegated user or session
  principal. Inference, service, and read-only OAuth credentials receive
  generic identity hints and an empty `keys` array; do not query sibling keys
  for that view. Mutation routes retain their separate fresh-proof checks.
- An omitted image model selects the exact advertised `FLUX.2 Klein 4B FP8`
  name. An explicit model or a style's model still takes precedence. Missing
  default capacity must fail before quota/dispatch, never silently switch models.
- Image generation accepts an optional strict integer `max_cost_micro_usd`
  (0..2^63-1). It limits the total discounted reservation across all credit
  pockets, not just purchased spend. A price above it returns 402 before funds
  or service exposure are reserved and before dispatch. Omission preserves
  existing callers. `/health.features` advertises `image-max-cost-micro-usd-v1`
  so agents do not mistake an older server's ignored extra field for protection.
  This is a per-request ceiling, not project budgeting or dispatch idempotency.

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

- `tests/test_video_jobs.py` covers request-key GET recovery with real scoped
  keys and SQLite/PostgreSQL, including missing/foreign/scope-rejected keys,
  no second quota/hold, admission rollback and redacted errors. The service
  journal tests additionally prove alias recovery and merged-key ambiguity.
  Synthetic distilled/dev normalized-intent fingerprints are pinned in Core
  and Director; neither fixtures nor matching digests prove a live paid CLI job.
  Ingredients coverage retains appearance/audio as distinct private roles with
  one test-priced hold, new same-worker feature and no opening-image slot.
  Replay after admission rollback uses neither another upload nor quota/hold;
  a changed appearance strength conflicts instead of repricing the accepted job.

- `tests/test_media_resume_process.py` kills real isolated Core after WS dispatch
  or a committed settlement, erases only its private Redis database, restarts
  with async admission disabled and proves same-worker recovery plus one hold,
  charge, result and ledger completion. Its dedicated worker credential also
  observes `held` before completion and `closed` after restart, including the
  lost-ACK case. `tests/test_worker_media_status.py` covers real scoped keys,
  SQLite/PostgreSQL rig isolation, legacy/expiry uncertainty, private errors,
  body bounds and absence of money writes. Scoped auth and worker registration are
  real; accelerated leases, worker output and R2 presence are fixtures. This is
  not the joint native-worker/GPU/R2 crash canary or a production rollout.

- `tests/test_validator_hostile_path.py` reuses the disposable Core process-crash
  fixture (`CREDITS_TEST_DB_URL`, local Redis). Real HTTP registration/scoped auth,
  assignments/probes, worker WS, signatures and quorum exercise malformed tools,
  truncation, ignored stops and truthful/false-vote controls. Wrong bindings,
  missing scope, stolen assignments and premature/conflicting votes reject.
  Signed false quorums remain stored but cannot pass the real compensation work
  verifier, frozen-campaign allocation or observation support selector. Actual
  campaign preview/apply/finalization consumes HTTP-created evidence, excludes
  false votes, allocates truthful controls and remains idempotent without creating
  payments. Worker output, operator reviews and accelerated finalization clocks
  are fixtures; no GPU identity, human independence or payment proof is claimed.
- `tests/test_validator_anti_gaming.py` includes 20 paired randomized synthetic
  controls per malformed-tool, truncated-output and ignored-stop fault. Correct
  template replies pass and corrupted replies fail. These are scorer tests, not
  real GPU/worker transport qualification or measured model-substitution recall.
- `pytest grid_api/routers/`.
- `tests/test_core_process_crash.py` starts the real Uvicorn application with
  scoped API keys, HTTP clients and worker WebSockets, a dedicated PostgreSQL
  database per case and a private Redis process. SIGKILL after reservation,
  dispatch or committed settlement is followed by restart against the same
  stores. Chat, Responses and Anthropic cover streaming and non-streaming;
  image, video and audio cover exact-charge recovery. Tests assert refund-once,
  original-job redelivery, no duplicate charge/reward and authenticated media
  recovery by job ID and client reference. Audio uses ephemeral signed wallet
  delegation/receipts. Worker output and R2 presence are simulated; clocks are
  shortened. This is not GPU qualification, a database/Redis-server crash,
  object-storage durability or elapsed production observation. Requires a
  disposable `CREDITS_TEST_DB_URL` with CREATE DATABASE permission and local
  `redis-server`; required PR/main CI provides both. No production DB is used.
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
- Registration retains `delegation_wallet` with the verified signer/ID/expiry.
  The service may reuse that exact enrolled identity for the same authenticated
  account and worker name after a payout-address change; worker registry payout
  metadata and settlement still use the current account destination. Run
  `grid_api/services/tests/test_worker_identity.py` for continuity, cross-account,
  cross-name, missing/tampered identity, signature, expiry and replay checks.
  `tests/test_core_process_crash.py::test_audio_identity_survives_payout_change_and_reconnect`
  also proves two reconnects through real Core/WebSocket/scoped authentication,
  PostgreSQL registry persistence and Redis after changing the reward destination.
- `pytest grid_api/services/tests/test_credits_billing.py` when changing any
  route that reserves, refunds, or reconciles credits.

## Child DOX Index

- `tests/` - router-level pytest coverage.
