# grid_api/services - dispatch, economy, safety, settlement

## Purpose

Business logic behind the routers: job dispatch, token streaming, the on-chain economy,
content sanitization, and reward settlement.

## Ownership

- **Dispatch:** `job_queue.py` (Redis streams - the ONE live queue), `token_stream.py`
  (worker->client token relay), `media.py` (image/video job abstraction), `storage.py`
  (presigned R2 upload), `enforcement.py` (worker strike/evict).
- **Economy:** `credits.py` (reserve/settle lifecycle; draws promotional, then
  daily free, then purchased value), `promotions.py` (durable budgeted grants,
  gated on `GRID_PROMO_SPENDABLE_LIVE` plus exact campaign IDs in
  `GRID_PROMO_SPENDABLE_CAMPAIGNS`), `free_credits.py`
  (daily free CREDIT allowance, Redis, FAIL-CLOSED, atomic consume/release
  idempotent on ref), `quota.py` (free-tier request COUNT, fail-open — distinct
  from credit value), `pricing.py` (versioned USD rates plus expiring,
  source-linked public same-model comparisons), `ledger.py` (incl. `content_hash` — real
  sha256 of witnessed output or NULL, never sha256("")), `den.py` (den
  accounting), `accounts.py` (scoped keys and payout preference),
  `identities.py` (verified identities, keyed subject lookups, aliases, and
  value-conserving merges),
  `user_tokens.py` (Core-issued short-lived sessions), `service_auth.py`
  (bounded service clients + proof exchange), `wallet_proofs.py` (EOA and
  deployed EIP-1271 personal-sign verification on Base), `service_limits.py` (fail-closed
  request/day ceilings), `alerts.py` (redacted, bounded operator event delivery),
  `assertions.py` (legacy app-only assertions), `economics.py`
  (splits, payout-asset + conversion-fee knobs, `worker_share_bps`),
  `canary_audit.py` (read-only account/job reconciliation for supervised
  demand-billing rollout),
  `validator_audit_budgets.py` (default-dark compensated-audit budget,
  terminal, and ledger-aware expiry lifecycle; the ordinary worker terminal
  imports it, but no scheduler can create audit work yet),
  `holdings.py` (cached on-chain AIPG balance + Chainlink ETH/USD),
  `deposits.py` (atomic Base funding receipts from verified account wallets
  plus USDC, bounded AIPG, and conversion-gated ETH claims),
  `x402_payments.py` (default-off accountless
  Base USDC authorization and settlement receipts), `model_registry.py`
  (ModelVault sync).
- **Remote MCP authorization:** `oauth_server.py` is the default-off OAuth 2.1
  authorization server for public MCP clients. It requires S256 PKCE, exact
  registered HTTPS or native-loopback redirects, short-lived resource-bound
  Grid user tokens, hashed one-use capabilities/codes, and a dedicated
  `oauth.introspect` backend scope. It does not issue refresh tokens or client
  secrets. `prune_operational_state` bounds authorization rows and removes only
  old clients that never completed a token exchange; used clients and economic
  account records are not retention targets. See
  `docs/architecture/REMOTE_MCP_AUTH.md`.
- **Worker trust:** `worker_identity.py` verifies a payout-wallet delegation to
  a funds-less per-rig signer plus a fresh registration proof; `signing.py`
  verifies that delegated signer over `aipg-job:{job_id}:{result_hash}`.
  Managed profiles and audio workers require identity now; the global identity
  gate remains a deliberate rollout for other Grid worker profiles.
- **Worker enrollment:** `worker_enrollment.py` coordinates a short-lived
  manager/Console pairing in Redis. The manager creates the final API key and
  poll secret locally; Core stores only their hashes, installs only
  `worker.connect`, and removes the key expiry only after manager ACK.
- **Validator account association:** `validator_pairing.py` owns optional,
  non-economic visibility links from an enrolled node to a separate human
  account. One ten-minute pairing slot per node, immutable browser approval,
  signer-bound local confirmation, and one SQL transaction for the link and
  terminal state. PostgreSQL locks the validator row and checks wall-clock
  expiry after lock acquisition. Either party may remove the exact association;
  old removal proofs cannot remove a later one. Retired accounts and rotated
  signers require deliberate re-pairing, never alias-following or key transfer.
  Private-list timestamps are timezone-aware on every supported database, so
  strict Console clients receive the same wire contract from SQLite and PostgreSQL.
  Pairing PostgreSQL tests use a unique schema per fixture and remove only that
  schema, including on failure; they must not drop shared Grid tables. A real-PG
  sentinel test proves unrelated data survives fixture cleanup. Keep the test
  database disposable despite this additional isolation.
  When the global flag is off, an unexpired configured pilot must include both
  canonical accounts. Node locks precede the availability/expiry recheck;
  existing links and pending approvals cannot bypass scope changes. Private
  listing filters out non-pilot nodes. Expiry blocks access but never mutates
  keys, balances, wallets, registration or stored links. Auth, fresh proof and
  node consent remain mandatory even for pilot accounts.
- **Validation evidence:** `validators.py` verifies one linked-wallet validator
  registration per canonical account, builds shared probe batches, binds one
  assignment and authoritative vote per registered validator/group, and
  aggregates a conservative 3-of-5 preview quorum. Assignment polls acquire
  PostgreSQL worker advisory locks in one canonical worker-ID order across
  validators so multi-worker polls cannot deadlock. Text groups use randomized
  exact-instruction, arithmetic, strict-JSON, calibrated 4K/16K/32K
  context-retrieval, multistep logic, restricted-AST Python function synthesis,
  exact function-call, two-stage tool-chain, stop-sequence, and gross token-limit
  families. The code scorer interprets only one bounded arithmetic return
  expression against assignment-only hidden inputs; it must never `exec`,
  import, call, or otherwise run worker-supplied code. Token-limit scoring uses
  Grid-side `o200k_base` counting over visible plus reasoning output, a
  length-style finish, and a documented cross-tokenizer tolerance;
  worker order must not determine the family. Multi-model workers rotate toward
  the least recently covered advertised model; a validator already assigned to
  an unfilled group for one model may cover another model, but cannot create a
  second group for the blocked model. New `text.generated.v8` batches
  fix one capability/canary lane but issue and persist a distinct randomized
  challenge for every validator. Prompt and expected-answer commitments are
  unique within the group by construction; generation retries boundedly and
  fails closed on repeated collisions. Already-open v7 groups drain with their
  shared challenge. Scorecards classify evidence as availability, protocol conformance,
  capability, quality, or fidelity. No current generated canary is quality-eligible.
  A regex/template solver may therefore pass protocol evidence but cannot earn a
  quality score. An accepted target-worker failure
  becomes economically inert failed evidence; a coordinator dispatch failure
  remains inconclusive. Group allocation
  must match the validator's advertised scorer capability; legacy
  `text.basic.v1` nodes receive only echo/arithmetic. Expected answers
  and Core's private verdict are not returned to nodes. The path remains non-economic and
  must not route production jobs, reward, slash, or write worker ledger rows.
  Non-economic does not mean resource-free: Core permits at most one new text
  group per worker/model per configured interval (one hour by default), stores
  the challenge once on the shared group, and hydrates validator-specific
  assignments at the API boundary. Finalized assignment/group machinery is
  pruned after the configured retention window; signed attestations remain the
  durable evidence record.
  Completed text probe envelopes include one bounded private `score_reason`
  code so maintainers can separate empty transport output, malformed protocol
  output, commitment mismatch, token-window mismatch, and latency without
  storing or exposing expected answers. Reason codes do not change the existing
  verdict, score, routing, or economic contracts.
  New probes stop at assignment expiry; already-completed probes may deliver
  only during the bounded attestation grace window. A completed assignment
  commits a JSON-safe synthetic result envelope (maximum 512 KiB) in the same
  state transition as `probe_status=completed`. Until that validator submits
  its authoritative vote, assignment polling returns the assignment and the
  targeted probe endpoint replays the stored envelope to that assignment's
  account/validator owner. It must not redispatch the worker or increment the
  attempt counter. Missing, oversized, or uncommitted results fail closed.
  `VALIDATOR_SEALED_ASSIGNMENTS_ENABLED` is a staged compatibility gate. When
  enabled, assignment polling returns only opaque lifecycle/capability fields
  and a SHA-256 commitment; the completed probe response discloses target,
  model, nonce, policy, and challenge. The public node must recompute that
  commitment before signing. Keep the flag off until every participating node
  supports both forms. A seal prevents advance API disclosure; it does not make
  public prompt families indistinguishable or authorize economic effects.
  Aggregate health may expose bounded counts, agreement/dispute rates,
  worker/model coverage, and software-version cohorts, but never validator
  identities. Scorecards label objective text votes as Core-matched or
  Core-disagreed and media/preview verdicts as validator opinion; a raw vote is
  never silently promoted to Core-verified fact. Independent-operator counts
  remain zero until externally reviewed; registration count is not independence
  proof. `validator_operators.py` owns the review state: an opaque control group,
  at least 72 hours of qualification, rate-limited heartbeat coverage, an
  expiring maintainer review, at least one completed probe plus authoritative
  attestation created during that qualification window, and preview-first
  compare-and-swap transitions. Evidence created before the candidate clock
  starts cannot satisfy the gate.
  A verify preview reports current qualification metrics and every blocking
  reason even before the gate is satisfied; an apply remains fail-closed until
  all blockers clear and requires that preview's exact state digest. Candidate
  re-entry cannot silently reset an active clock; apply requires an explicit
  reset acknowledgement, and operators must use it on both preview and apply.
  A control group occupies at most one seat in any probe group. Public health
  exposes only distinct aggregate group counts, never group ids or review refs.
  The account-authenticated registration view exposes that operator's own
  qualification status, sampled-heartbeat progress, review expiry, and current
  eligibility, but never its opaque control-group id or private review ref.
  Public validator lookup may expose only the shareable validator ID, a
  minute-rounded heartbeat, software version plus the frozen required cohort
  version and compatibility status, aggregate assignment/attestation counts,
  redacted qualification progress, and a bounded next action. An online stale
  version must receive an upgrade action before cohort-review guidance. Wallets,
  account IDs, signatures, operator groups, review refs, raw assignments, and
  evidence remain private.
  Candidate and verify transitions require the frozen cohort version from typed
  Core settings. Software version is part of the compare-and-swap review digest,
  so a re-registration or downgrade between preview and apply invalidates the
  transition. Rejection remains available regardless of version.
  `validator_text_calibration.py` provides the same privacy boundary for
  read-only text-lane calibration: only public model names, lane/status/verdict,
  bounded reason/finish codes, counts, and average latency may leave the
  service. It must never return prompts, outputs, nonces, assignment, worker or
  validator identifiers, wallets, accounts, or signed evidence, and its report
  has no quality, routing, or economic authority.
  `validator_cohort_monitor.py` owns the aggregate, read-only cohort watchdog.
  It measures matured assignment completion, authoritative evidence delivery,
  terminal probe errors, disagreement, stale active/candidate registrations,
  frozen-baseline version drift, and duplicate reviewed control groups. Its
  Redis lease permits one monitor across all Uvicorn processes and Core
  replicas; a Redis fault skips the pass rather than multiplying query loops.
  The lease has no qualification or authority effect. Its
  output and alerts contain counts only, never validator IDs, group IDs, review
  refs, wallets, accounts, prompts, responses, or evidence. It has no routing,
  reward, strike, payout, qualification, or slashing side effect.
  `validator_shadow.py` owns the separately gated seven-day advisory observer.
  It derives evidence only from Core-reverified finalized assignment,
  attestation, signature, nonce, evidence-hash, frozen-version, heartbeat, and
  expiring operator-review bindings. Duplicate operator groups count once.
  Its public observation function never accepts caller-asserted binding
  validity. Shadow runs, observations, outcomes, Core-derived capacity samples,
  and bounded errors are private replay records only; routing, worker health,
  credits, settlement, payouts, rewards, bonds, strikes, and slashing must
  neither import this module nor query its tables. The flag defaults off, the
  CLI is read-only, and a completed run cannot promote itself. Missing expected
  sample slots and successful ledger completions absent from the route capture
  count against report coverage. `route_events.py` is the neutral, non-awaited
  producer: after actual dispatch it HMAC-commits job/Redis-delivery identity,
  reduces the job to bounded metadata before scheduling background work, uses a
  two-second single-flight cache of minimal worker fields to snapshot compatible
  replicas, caps snapshots at 256 candidates and 128,000 UTF-8 bytes, and writes
  no prompt, output, account, wallet, worker name, or validator identity.
  `validator_shadow_collector.py` is the only outbox consumer allowed to import
  `validator_shadow`; it uses a Redis lease, retries transient faults, records
  outcomes/capacity, and has no routing or economic output. The Redis outbox has
  a 10,000-event emergency bound; SQL retention configuration remains reserved
  until an append-only-compatible pruner is designed and tested.
  Preview group acceptance still records distinct-registration quorum for
  compatibility; independent-operator quorum is a separate explicit signal and
  is not required for acceptance until a later reviewed authority gate.
  The public network status surface uses only these redacted aggregates and
  never assignment rows.
  `worker_control_reviews.py` owns preview-first, digest-bound, expiring common-
  control reviews for media workers. Its opaque `opg_*` groups are private,
  identity-bound operational metadata and have no routing, payout, reward, or
  slashing effect. `validator_references.py` owns the dark media reference
  selector: fresh finalized bond + quality snapshots, online workers, three
  fresh distinct worker-control groups across candidate and references,
  distinct accounts and payout wallets as defense in depth, row-locked recent-
  use rotation, and fail-closed insufficiency. Pairwise account, payout-wallet,
  and control-group independence must be rechecked from the authoritative rows
  after their transaction locks are acquired; the initial candidate snapshot
  is not sufficient.
  `validator_media_readiness.py` is the read-only rollout preflight. It uses the
  selector's non-mutating preview to combine live recipes, workers, fresh
  validator capabilities, externally reviewed operator independence, and
  candidate-specific reference quorum into a redacted advisory report. A green
  report never bypasses the transactional selector or grants economic authority.
  The default-off deterministic image lane calls it transactionally, runs one
  candidate plus two fixed references exactly once per shared probe group
  through hard-targeted ordinary UUID jobs, freezes worker uploads under
  Core-only R2 keys, and returns one response-committed set of Core-computed
  SHA-256 witnesses to every validator assignment. It requires an on-chain
  recipe id, deterministic metadata,
  governed model digest, explicit bond policy, and `image.fidelity.v1` validator
  capability. The independent default-off `video.fidelity.v1` lane applies the
  same bond, common-control, deterministic-recipe, model-digest, and two-reference
  gates to text-to-video recipes with explicit prompt/seed/dimensions/timing. It
  hard-targets one candidate plus two references, freezes three MP4 witnesses,
  and requires reference agreement before candidate comparison. Both lanes
  remain non-economic.
  `validator_bonds.py` owns the default-off Base cache refresh. It verifies all
  WorkerRegistry selectors route through the reviewed Grid Diamond to one
  code-pinned facet release at one mutually finalized block, requires two distinct
  RPC sources to return the exact same block hash and complete snapshot, bounds
  reads to the payout wallets in pre-existing reviewed reference rows, and never
  scans the global append-only worker history. It never creates/activates a
  reference, mutates quality review, or touches economics. A durable
  authority-scoped cursor anchors later reads to the prior finalized block hash;
  a PostgreSQL advisory transaction lock serializes Core replicas. Any sync
  ambiguity atomically marks that cursor faulted and clears eligibility only for
  that authority until a later exact sync recovers it. Operators may select only
  a verifier/runtime pair compiled into the Core release.
  `validator_reference_reviews.py` owns the preview-first, digest-bound human
  quality and status workflow. A new review starts paused and cannot activate
  until its fresh chain proof and worker-control review exactly match the live
  Core policy. Identity drift forces a pause and clears cached proof fields;
  only the bond sync may repopulate them. Revocation is terminal.
- **Model/media governance:** `recipes.py`, `recipe_import.py`, `styles.py`,
  `recipe_vault_sync.py`, `loras.py`, `model_registry.py`. RecipeVault sync is
  default-off and accepts only a dual-RPC-agreed finalized snapshot whose exact
  ten selectors route to a Core-pinned runtime. Public records must be
  uncompressed Core-canonical JSON with matching SHA-256 roots and valid bounded
  metadata. Stage the complete snapshot before atomically replacing the chain
  cache. A stale authority drops chain recipes but retains root/name tombstones:
  unrelated reviewed local recipes remain, while governed recipes fail closed.
- **Safety:** `sanitizer.py` - **secrets redactor only** (strips API keys/PGP from prompts).
  NOT a content filter.
- **Settlement:** `settlement/` - owned in its own AGENTS.md.
- **Deferred decentralized dispatch:** `p2p/` - owned in its own AGENTS.md and
  default-off.
- **Tests:** `tests/` - service-level pytest coverage.

## Local Contracts

- One queue: `job_queue.py`. Requeue is capped (Redis counter, dead-letter at
  the cap) to prevent poison-job eviction cascades. Compatible-worker generation
  failures use a tighter two-requeue budget than heterogeneous model-mismatch
  bounces. Stale jobs are reclaimed by the loop in `main.py`.
- Money paths must stay idempotent and tested; value-moving credit ledger writes
  require non-null refs and must not overdraft under concurrency.
- Compensated validator audits reserve integer work units against four locked
  PostgreSQL scopes: global, worker, reviewed validator, and validator/worker
  pair. The ordinary worker terminal appends its payout ledger row and settles
  the audit hold in the same transaction; it must commit before client/worker/
  queue success acknowledgement. Expiry releases only holds without a payout
  row; a conflicting row moves to manual review. Existing demand reservations
  and audit holds may never share a job UUID. Validator registration alone is
  insufficient: a current independent review, fresh heartbeat, and explicit
  signing-wallet allowlist are mandatory. No scheduler or public endpoint may
  create compensated work until the separate configuration/dispatch gate lands.
- A successful Base funding claim atomically writes its immutable
  `grid_deposits` receipt and purchased-credit ledger movement. AIPG valuation
  must use a fresh operator epoch plus hard transaction/account/network caps;
  do not derive credit from the thin pool's spot price. Deposit claims are the
  narrow exception to the no-request-path-chain-read rule: they must verify the
  configured RPC is on the expected chain before trusting transaction/receipt
  data, and they must never sit in the inference hot path.
- Production ETH funding uses `swap_receipt`: the linked wallet spends native
  ETH and the confirmed transaction must deliver canonical Base USDC directly
  to the configured USDC treasury. Credit only the actual USDC Transfer amount.
  The oracle-priced `buffered` mode is an operator-only pilot, not a public
  funding path.
- AIPG funding claims must bind the transfer block timestamp to the active
  operator price epoch. Never value a historical transfer under a newer epoch.
- x402 authorization is not revenue. Its reservation and verified-payment row
  commit before dispatch; its exact attempt is persisted as `settling` before
  the facilitator can touch chain. Facilitator success is only `reported`;
  worker payout aggregation must exclude that job until Core independently
  proves the exact canonical-USDC Base transfer and records `settled`.
  Ambiguous attempts go to `manual_review`. The initial route is Base USDC,
  `upto`, text-only, and non-streaming because the upstream middleware buffers
  the response before settlement.
- Media billing reserves exact deterministic cost before dispatch and refunds on
  non-running paths; text billing reserves max cost and reconciles against trusted
  usage.
- Batch is accepted only for workflows with a verified batch strategy. Until
  recipe metadata carries that contract, video and source-image jobs are
  single-output and must reject larger `n` before quota, upload, reservation,
  or dispatch.
- After a media queue submission attempt, only the worker terminal,
  dead-letter path, or stale-reservation sweeper may settle/release the hold.
  An HTTP timeout or disconnect is not proof that GPU work stopped.
- Successful image, video, 3D, and audio responses expose the Core-generated
  `grid.job_id`. Consumer applications should retain it as the immutable handle
  joining the generation to the completion and credit ledgers.
- Media storage requires an explicit `R2_TRANSIENT_BUCKET`; missing storage
  configuration fails closed and must never fall back to a repository-embedded
  operational bucket name.
- **Three credit pockets, never converted:** charges draw promotional, daily
  free, then purchased value when each pocket's gate is live. The split is
  durable in `grid_reservations.promo_micro/free_micro`, and settlement restores
  each pocket to itself. Paid movements commit in the SQL txn; the Redis free
  restore follows the commit (a crash between forfeits free-day allowance,
  never paid money). The stale-reservation sweeper inherits this via
  settle_job/release_job/settle_exact.
- Reviewed builder grants use immutable `builder-*` campaign contracts and the
  same promotional pocket. They are issued only by the dry-run-first operator
  command, remain globally budgeted and expiring, and cannot be self-claimed
  through a public endpoint.
- A wallet is not Sybil resistance. The welcome campaign requires a verified
  Google identity and has a finite global budget; wallet-only accounts do not
  receive it. The daily baseline also requires verified Google. Holder value
  defaults to zero until qualification cannot be recycled between wallets.
- Account merges require proof of both sides, refuse active holds, revoke source
  keys, preserve accrued payout reachability, and move purchased balance through
  paired append-only ledger entries.
- Identity subjects use a server-keyed, domain-separated digest. Runtime lookup
  accepts the historical unkeyed SHA-256 form only to preserve existing logins
  and atomically upgrades a proved legacy row. Never remove that dual-read path
  until production has evidence that no legacy identity rows remain.
- Deposit senders must match a verified wallet identity on the canonical
  account. Never accept a client-supplied funding address without checking its
  verified identity hash. A transaction or receipt that the configured RPC has
  not seen yet is retryable (`425`), not an invalid claim; clients must retry
  the same transaction hash and must never resend value to recover credit.
  Funding configuration may expose advisory daily-cap usage for preflight, but
  `_record_and_credit` remains the authoritative locked enforcement point.
- Service keys remain long-lived backend credentials but cannot manage user
  accounts. Global Google/SIWE proof is verified by Core; partner SIWE is
  additionally bound to the service's exact allowlisted domains and optional
  service-local subject. App delegation is namespaced to one service and
  receives bounded inference authority. The service must derive any exchanged
  app subject from its authenticated server session, never directly from an
  untrusted request field. A normal service key cannot submit inference as its
  own account: it must include a service-bound short-lived user token. The
  `inference.service_submit` scope is an explicit exception for capped,
  service-owned workloads such as bots and demos; provisioning requires positive
  per-request and daily ceilings, and key rotation preserves the existing scope
  set.
- Native service exchange uses the stable service-id namespace. During
  migration it also resolves the former service-account-UUID namespace used by
  signed assertions, attaches the stable identity, and value-conservingly
  merges conflicts. Never remove this compatibility path while legacy
  assertion identities or balances remain.
- Partner wallet exchange supports EOAs without an RPC call and deployed
  EIP-1271 contract wallets through `eth_getCode` plus `isValidSignature` on
  Base. RPC failure rejects the contract-wallet proof; it must never fall back
  to trusting the partner's assertion.
- The free request-count quota exempts positive purchased-credit accounts and a
  direct service key only when both its per-request and daily micro-USD ceilings
  are positive. Promotional/daily-free value, delegated users without purchased
  credit, and unbounded service keys remain in the free-user quota path.
- Service ceilings are exposure reservations keyed by job id: reserve before
  dispatch, reduce to actual text spend on success, and release on 402/no-work
  terminals. Redis failure stays conservative until the day bucket expires.
- Price coverage is modality-specific. A model entry with only a video rate is
  unpriced for image/text, and positive quotes round up to one micro-USD rather
  than silently becoming free.
- New monetary holds obey `off | allowlist | on`; an existing durable hold must
  still settle or refund after the operator disables new charging.
- Charging allowlists select delegated/user work by canonical account. Service
  IDs select only direct service principals carrying
  `inference.service_submit`; never let a frontend service allowlist charge all
  of its delegated users.
- Operator alerts are best-effort and data-minimized. Never include prompts,
  outputs, email addresses, credentials, signatures, raw exceptions, or
  unredacted identity values in alert fields.
- Text reservations snapshot input/output rates and holder discount at reserve
  time. Never reprice an in-flight job from the current price book.
- `ledger.py` writes one completion event per job. Settlement and stats depend on
  `grid_ledger`; do not revive orphan den tables for new v2 payouts.
- On-chain reads only via sync loops, cached; never per-request.
- Never copy a payout private key to a worker. Core resolves the payout wallet
  from the API-key account, then verifies its signed delegation to the worker's
  local signer. Registration nonces are one-use and fail closed if Redis is down.
- Managed profile metadata is not self-authenticating. Core accepts an
  allowlisted release digest only with Core-owned profile ID, runtime adapter,
  runtime digest, recipe root, and capability-tier values. Runtime execution
  still requires validator evidence.
- Enrollment create/poll endpoints never return a plaintext API key. Keep
  request secrets as `SecretStr`, preserve Redis TTLs, and keep completion
  idempotent across browser retries and manager crash-resume. Payout-wallet
  binding and temporary worker-key insertion must commit atomically.
- `model_registry.py` is not currently wired into startup. Do not claim
  ModelVault enforcement is live unless the sync is wired and tested.
- `enforcement.py` records slashable evidence only; it must not directly slash
  bonded funds from a hot request path.
- Validator attestations and scorecards are evidence only until reward/dispute
  rules exist. A submitted or aggregated `failed` verdict is not a worker strike
  by itself.
- `router.py` must not read validator attestations into model or replica scores.
  Until blind quality evidence and a reviewed activation policy exist, `auto`
  routing may use curated tiers plus Grid-measured throughput/latency only.
- Random challenge values prevent answer replay, not template recognition. Do
  not describe generated canaries as blind workload validation or proof of a
  model family. Protocol-conformance evidence must remain separate from
  capability and quality evidence.
- Assignment polling must not disclose target, model, nonce, or challenge once
  sealed mode is enabled. The terminal disclosure must be canonically committed
  before execution and must remain identical on result replay. Do not remove
  the unsealed response until the public validator fleet has upgraded.
- Authoritative validator evidence requires a Grid-issued assignment id, nonce,
  and matching probe evidence hash. Preview/local evidence stays visible only as
  preview.
- Validator attestation identity is evidence identity only, but must still be
  coherent: malformed validator wallet strings are rejected, signed evidence
  requires a claimed wallet, and stored validator wallets are normalized
  lowercase.
- A validator registration wallet must be the verified wallet on the key's
  canonical account. One account has one validator identity; one validator has
  one assignment and one authoritative attestation per shared probe group.
  Distinct registrations do not prove independent operators, and preview
  quorum has no economic authority.
- A validator may self-suspend only with a fresh signature from its registered
  wallet. Signing-wallet rotation is account-recovery, not a new validator: it
  preserves the validator ID, requires a different replacement wallet already
  linked to the same canonical account, and requires that wallet's fresh
  signature. A revoked validator cannot rotate or self-reactivate.
- Targeted validator probes use an atomic, bounded assignment lease. Concurrent
  calls cannot dispatch duplicate free inference; expired leases are
  reclaimable, and late results cannot overwrite the current attempt.
- Validator media uploads are mutable transport only. Core must copy each
  accepted upload to a key for which no worker PUT URL exists, hash that frozen
  object itself, and delete the source before exposing a witness. Media probes
  must never call credit, ledger, den, strike, payout, metrics, or bond mutation
  paths.

## Work Guidance

- Adding economic logic -> add/extend tests under `tests/` or `settlement/tests/`.
- Safety work should be a layered pre/post-dispatch content policy; do not
  overload `sanitizer.py`.
- When adding env-driven behavior, prefer centralizing in `grid_api/config.py`
  over scattered `os.getenv`.
- Keep synchronous Web3/R2/network work off the event loop; use startup loops,
  offline jobs, or `asyncio.to_thread` as appropriate.
- Bond-sync failure must leave the last snapshot untouched so freshness expiry
  fails closed. Never partially persist an RPC traversal or accept a stale block
  over a newer cached block.

## Verification

- `pytest grid_api/services/` - covers `job_queue`, `den`, `quota` (+ settlement subtree).

## Child DOX Index

- [p2p/AGENTS.md](p2p/AGENTS.md) - default-off P2P decentralization prototype.
- [settlement/AGENTS.md](settlement/AGENTS.md) - Merkle settlement + IPFS + aggregation.
- `tests/` - service unit tests (job_queue, den, quota).
