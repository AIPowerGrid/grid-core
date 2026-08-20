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
  gated on `GRID_PROMO_SPENDABLE_LIVE`), `free_credits.py`
  (daily free CREDIT allowance, Redis, FAIL-CLOSED, atomic consume/release
  idempotent on ref), `quota.py` (free-tier request COUNT, fail-open — distinct
  from credit value), `pricing.py`, `ledger.py` (incl. `content_hash` — real
  sha256 of witnessed output or NULL, never sha256("")), `den.py` (den
  accounting), `accounts.py` (scoped keys and payout preference),
  `identities.py` (verified identities, aliases, and value-conserving merges),
  `user_tokens.py` (Core-issued short-lived sessions), `service_auth.py`
  (bounded service clients + proof exchange), `wallet_proofs.py` (EOA and
  deployed EIP-1271 personal-sign verification on Base), `service_limits.py` (fail-closed
  request/day ceilings), `alerts.py` (redacted, bounded operator event delivery),
  `assertions.py` (legacy app-only assertions), `economics.py`
  (splits, payout-asset + conversion-fee knobs, `worker_share_bps`),
  `canary_audit.py` (read-only account/job reconciliation for supervised
  demand-billing rollout),
  `holdings.py` (cached on-chain AIPG balance + Chainlink ETH/USD),
  `deposits.py` (atomic Base funding receipts from verified account wallets
  plus USDC, bounded AIPG, and conversion-gated ETH claims),
  `x402_payments.py` (default-off accountless
  Base USDC authorization and settlement receipts), `model_registry.py`
  (ModelVault sync).
- **Worker trust:** `worker_identity.py` verifies a payout-wallet delegation to
  a funds-less per-rig signer plus a fresh registration proof; `signing.py`
  verifies that delegated signer over `aipg-job:{job_id}:{result_hash}`.
  Managed profiles and audio workers require identity now; the global identity
  gate remains a deliberate rollout for other Grid worker profiles.
- **Worker enrollment:** `worker_enrollment.py` coordinates a short-lived
  manager/Console pairing in Redis. The manager creates the final API key and
  poll secret locally; Core stores only their hashes, installs only
  `worker.connect`, and removes the key expiry only after manager ACK.
- **Validation evidence:** `validators.py` verifies linked-wallet validator
  registration, binds assignments to registered nodes, verifies signed
  assignment evidence, tracks non-economic workflow states, and builds aggregate
  scorecards. Authoritative evidence must match the registered wallet,
  Grid-issued assignment id/nonce, and hard-targeted probe evidence hash. These
  states are not independent-validator quorum and must not route production jobs,
  reward, slash, or write worker ledger rows.
- **Model/media governance:** `recipes.py`, `recipe_import.py`, `styles.py`,
  `loras.py`, `model_registry.py`.
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
- A wallet is not Sybil resistance. The welcome campaign requires a verified
  Google identity and has a finite global budget; wallet-only accounts do not
  receive it. The daily baseline also requires verified Google. Holder value
  defaults to zero until qualification cannot be recycled between wallets.
- Account merges require proof of both sides, refuse active holds, revoke source
  keys, preserve accrued payout reachability, and move purchased balance through
  paired append-only ledger entries.
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
- Authoritative validator evidence requires a Grid-issued assignment id, nonce,
  and matching probe evidence hash. Preview/local evidence stays visible only as
  preview.
- Validator attestation identity is evidence identity only, but must still be
  coherent: malformed validator wallet strings are rejected, signed evidence
  requires a claimed wallet, and stored validator wallets are normalized
  lowercase.
- A validator registration wallet must be a verified wallet on the key's
  canonical account. One registered validator may submit at most one
  authoritative attestation per assignment. Shared-challenge quorum remains a
  separate future protocol.
- Targeted validator probes use an atomic, bounded assignment lease. Concurrent
  calls cannot dispatch duplicate free inference; expired leases are
  reclaimable, and late results cannot overwrite the current attempt.

## Work Guidance

- Adding economic logic -> add/extend tests under `tests/` or `settlement/tests/`.
- Safety work should be a layered pre/post-dispatch content policy; do not
  overload `sanitizer.py`.
- When adding env-driven behavior, prefer centralizing in `grid_api/config.py`
  over scattered `os.getenv`.
- Keep synchronous Web3/R2/network work off the event loop; use startup loops,
  offline jobs, or `asyncio.to_thread` as appropriate.

## Verification

- `pytest grid_api/services/` - covers `job_queue`, `den`, `quota` (+ settlement subtree).

## Child DOX Index

- [p2p/AGENTS.md](p2p/AGENTS.md) - default-off P2P decentralization prototype.
- [settlement/AGENTS.md](settlement/AGENTS.md) - Merkle settlement + IPFS + aggregation.
- `tests/` - service unit tests (job_queue, den, quota).
