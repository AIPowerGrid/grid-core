# grid_api/services - dispatch, economy, safety, settlement

## Purpose

Business logic behind the routers: job dispatch, token streaming, the on-chain economy,
content sanitization, and reward settlement.

## Ownership

- `video_payment_envelope.py` owns the private strict x402 v2 outer boundary.
  Derive exact amount/asset/network/payTo/domain/timeout/EIP-3009 method from
  the immutable live quote, never client requirements. Freeze quote/envelope
  inputs before validation/serialization so caller mutation cannot rebind them.
  Binding-v3 commits upfront/EIP-3009 terms; wire `extra.paymentFlow=upfront`
  requires settle then independent receipt proof before job admission, without
  facilitator `/verify`. Locked 2.16.0 request-shape compatibility does not
  qualify actual client/facilitator flow handling.
  Validate primitive types and exact fields before SDK construction; reject v1, extensions,
  coercion and client-selected resource metadata. Optional HTTPS descriptor
  must match an operator-controlled URL, never Host/request authority. Bounded
  canonical PAYMENT-SIGNATURE decoding rejects duplicate JSON, floats/nonfinite
  numbers, invalid UTF-8 and deep/oversized input. Original inner cryptographic
  binding still applies. The SDK-shaped facilitator body contains a spending
  capability: do not log/persist it, or send without the winning committed
  attempt and ready inputs. No route, client, signer, SQL or admission calls
  this helper; validation never proves revenue or grants generation.
- `video_refunds.py` owns private `0052` reviewed Safe refund intent/recovery.
  Only an owned proved receipt with a committed refund obligation can prepare
  one nonce/amount/payer. Active and completed paid jobs cannot refund. Existing
  account aliases or payout-wallet changes cannot redirect the original payer.
  Chain/Safe/nonce uniqueness fences competing obligations. Preparation grants
  no send permission; the operator must review the nonce and other Safe traffic.
  Stored nonces are never replaced automatically. Proof runs outside SQL locks;
  outbound `video_x402`/`refund:<quote UUID>` receipt ownership and confirmed
  refund status commit together. Failures/conflicts stay unresolved, never a
  reason to resend. Original prepaid refund-due/zero-charge state stays intact,
  so returned money cannot mint credit or worker backing. No route/signer/loop
  invokes this service. Retain all history on rollback, including preparations.
- `video_refund_proofs.py` owns unsigned single-transfer Safe 1.4.1 intents and
  read-only dual-source proof. Freeze Call-only canonical USDC transfer, zero
  native value/gas reimbursement, original payer/amount, Safe nonce and EIP-712
  hash. Require exact direct execution calldata, one matching ExecutionSuccess
  with the transaction hash in its indexed topic and zero payment in data
  after one exact Transfer, status, code/version/decimals/hash/consumed-nonce
  views at the required-canonical block and re-read confirmation anchors.
  Unknown versions, modules, batches, delegatecall and gas-token reimbursement
  reject. It shares existing bounded RPC deadlines/replies/slots and writes no
  state or signatures. Version/state reads are not general runtime attestation;
  actual treasury proxy/singleton/guard/modules, pending nonce coordination,
  Safe UI and real token/refund qualification remain gates. Sources must be
  operator-selected TLS-verifying clients; two hosts are not chain proof.
- `tests/video_fork_support.py` and `tests/test_video_payment_fork.py` own
  opt-in local Anvil/Base contract-execution qualification. Writes require
  a literal loopback URL and repeatedly checked Anvil/pinned-fork identity.
  Fresh payer keys and local owner impersonation never access production keys
  or submit to the upstream fork source. Real Safe/USDC code, SQL admission,
  failure obligation/refund closure, consumed authorization and both replay
  reverts are exercised; provider failures cannot count as replay rejection.
  Snapshot rollback runs in finally. Two logical proof adapters share one
  fork, not independent providers; this is neither actual Base payment nor
  GPU/storage/customer-funded qualification. Inputs/prices remain synthetic.

- `video_prepaid.py` owns private `0051` proved-receipt admission. Actual shared
  ownership, proof, ready original inputs and account/quote/job locks precede
  one exact external hold plus frozen outbox/input-authority transaction.
  Closed/expired/capacity-rejected admission instead records an unsent USDC
  refund obligation without an executable job. Neither outcome draws credit.
  Existing worker terminal/release/sweep/fence/backing code recognizes this
  distinct source; failed jobs owe USDC, never restored credit. Only a committed
  verified result earns exact backed worker consumption. Replay/read recover
  one immutable decision; altered graph/source/receipt bindings fail closed.
  Admitted inputs use existing post-closure cleanup, not inferred URL authority.
  Unadmitted/uncertain-payment input cleanup, live storage/chain/GPU and actual
  reviewed refunds remain gates. No endpoint, facilitator, signer or loop
  invokes this admission. Apply `0051` before all candidate financial/backing
  readers, retain prepaid-aware handlers/history on rollback, and keep public
  model permission and HTTP payment collection separate.

- `video_contract.py` owns the candidate strict LTX-2.5 execution/timing
  contract. Only recipes declaring `_grid.videoContract` use it. Qualified
  graphs are snapshotted before reserve; steps/frames/duration and quote units
  must match dispatch. `recipes/qualification/` is not loaded at startup.
  Typed pins are metadata, not executable nodes: the resolver expands the
  governed first-image scaffold into a fixed LTX guide chain. Distilled permits
  up to 337 frames, dev 81, both at most sixteen ordered pins starting at zero.
  Source lists and bindings must agree before reserve; multi-pin dispatch requires
  `recipe-image-bindings-v1`. Old workers requeue without receiving the graph;
  mismatch exhaustion releases the hold through the existing terminal path.
  Distilled candidates may declare audio/video source modes. Core expands only
  its fixed frozen-audio/Union-Canny scaffold, independently of optional opening
  image pins. A character portrait is not identity-only conditioning.
  The distinct `ingredients-dev-v1` appearance profile is fixed to Dev BF16,
  768x448/121 frames/24 fps/40 steps/CFG2. It requires a separate appearance PNG
  and exact-duration audio, rejects opening pins, and chains Ingredients before
  optional Union motion control. Standard Dev remains capped at 81 frames.
  Appearance/motion strengths are committed execution controls, not node or
  adapter selectors; no portrait-at-frame-zero or identity-fidelity promise.
  Optional finite `reference_attention_strength` in (0, 1] changes only the
  appearance guide to the governed Advanced node. Omission preserves its
  original graph and intent; neither denoising nor motion attention changes.
  Both source inventories require that node before worker advertisement.
  `normalize_timing` shares exact frame/duration validation with execution and
  non-mutating credit quotes. A quote needs no private appearance/audio bytes;
  it cannot validate sources, compile a graph or grant admission. Full
  `normalize` still rejects missing appearance/audio inputs. Ordinary Dev's
  frame limit and the fixed Ingredients bucket remain unchanged.
- `video_sources.py` owns bounded inline WAV/MP4 input decoding, exact-duration
  audio validation and isolated FFmpeg probe/full-frame checks. It accepts no
  input URLs or filenames. Validate all inputs before quota/upload, retain
  hash/size/role/timing commitments and require them to match the compiled
  source slots before a hold. The Ingredients-only `reference_image` role is
  a fully decoded, nonanimated 768x448 PNG under 12 MiB with separate byte/hash/
  dimension commitments; it never enters the opening-image source list.
  MP4 verification has two process-local slots;
  cancelled callers retain their slot until their decoder task finishes.
  FFmpeg time/individual-allocation limits are not a complete OS sandbox.
  Source retention/privacy and joint native/billing qualification remain gates.
  Governed audio/video sources and typed LTX image pins use
  `storage.upload_private_source`, never the output bucket. Its typed private
  bucket must be configured separately; the native route preflights that
  requirement before quota/uploads. Uploads return bounded one-hour GET links.
  Private-bucket public-access/lifecycle review is an operator gate; URL expiry
  is not object deletion. Legacy `upload_source` still uses the public output
  bucket and must not be described as private merely because its URL is signed.
- `media_source_retention.py` records at most eighteen exact Core-uploaded
  private input keys with the journal/hold transaction.
  Ingredients appearance/sound/motion may record three ordered source roles,
  exclusively without opening pins; ordinary source collections remain capped
  at two. The total eighteen-object authority is unchanged. Only a committed settled
  reservation (paid or refunded) permits cleanup. Historical payloads are never
  backfilled into deletion authority. A separate minute loop deletes bounded
  batches with time-limited S3 calls and retries exact keys after crashes or
  partial failure. Cancellation retains its single-flight guard until storage
  work finishes. Failures rotate behind other pending jobs, not starving them.
  Cleanup markers are operational metadata; no credits, results or ledgers move.
  It continues after admission rollback. Retain old bucket permissions until
  their recorded jobs drain; changing the S3 account or making a recorded bucket
  the output bucket refuses deletion. ComfyUI files, output/attempt orphans and
  pre-admission uploads are not cleaned by this service. The provider source
  lifecycle remains required. Apply migration `0045` before candidate startup.

- **Dispatch:** `job_queue.py` (Redis streams - the ONE live queue), `token_stream.py`
  (worker->client token relay), `media.py` (image/video job abstraction), `storage.py`
  (presigned R2 upload), `enforcement.py` (worker strike/evict).
- `media_jobs.py` owns the private journal for default-off async video ingress
  and the bounded background outbox. A UUIDv4 idempotency key is hashed, bound to
  the canonical account and normalized-intent hash, then retained with one
  immutable compiled graph. PostgreSQL serializes pocket draws; the existing
  credit hold and journal insert commit together. Live positive credit holds and
  explicit cost caps are required; dry-run never writes an async job.
  The canonical-account lock also serializes the held async-job cap (default
  four, at most sixteen), including proved aliases. This is separate from the
  synchronous media/text concurrency limits; replay does not consume a slot.
  Unclaimed queued work can be republished after lease expiry, including after
  Redis loss. Each queue attempt has a five-second maximum; six attempts exhaust
  to an unstarted-only release. The outbox continues after admission rollback.
  Outbox retries may create duplicate Redis entries. Before any marked job's WS
  dispatch, the worker handler must commit the SQL execution fence under the
  existing financial job lock. Never clear that fence on reconnect, requeue or
  rollback. Uncertain execution awaits reconciliation/refund, not a rerender.
  Financial/results authority remains `grid_reservations`; no second settler.
  Recovery reads accept exactly one owned job UUID or hashed request key and
  expose the normalized intent digest, never key text or private payloads.
  Request-key recovery is a read, not another admission or retry. Proved
  aliases remain recoverable; merged duplicate keys fail as ambiguous.
  Queue give-up may release a marked job only with `only_if_unstarted_media`
  under that same job lock. The current leased terminal or stale-hold sweeper can still release;
  a duplicate delivery must not refund another worker's running hold.
  Candidate original-worker continuation selects only held executions bound to
  the authenticated worker UUID, exact selected checkpoint and required features.
  A 60-second renewable private Core lease fences both success and failure in
  the money transaction; replaced/expired sessions move nothing. Renewal stops
  at 30 minutes from the original claim, never restarting that budget. Resume is
  sent directly with `resume:true`, the original job/graph/seed and fresh,
  attempt-isolated upload slots; a missing local journal must not rerender.
  Pre-0044 NULL leases remain uncertain, never backfilled. Recovery works after
  new-admission rollback and Redis loss. Legacy workers perform no recovery query.
  Public enablement, joint worker/GPU/R2 crash canaries, retained inputs/results
  and bounded private cache retention remain release gates. Old workers must not advertise
  `async-video-resume-v1`. See `LTX_2_5_VIDEO.md`.
- `video_output.py` owns candidate async video byte verification only. It accepts
  one exact Core-issued attempt slot, bounds the MP4 to 256 MiB, and copies it
  under a unique Core-only final key with the observed source ETag condition.
  Core streams/hashes that copy directly through S3 before the money terminal.
  Two active verifications per process, bounded reads/retries and a 120-second
  verification budget constrain resource use. Invalid bytes release only the
  current lease's hold; storage/conditional-copy uncertainty retains the hold
  without payment or ACK. The paid result and receipt use the verified copy,
  never a worker-writable PUT slot. This does not prove codec/timing, creative
  quality, model fidelity, production R2 behavior or retention. Ambiguous
  copies may orphan unique objects; private cleanup/lifecycle is a release gate.
  Legacy synchronous image/audio/video and validator storage are unchanged.
- `video_plans.py` owns private in-memory validated inputs and immutable
  `aipg.video-plan.v1` execution snapshots. Async ingress validates sources and
  compatible-worker admission, then compiles one graph/seed/units/source digest
  before quota or storage. Planning uses non-routable source slots; only their
  locations change during staging. Keep source bytes out of serialized snapshots
  and reprs. Recheck commitments before uploads and copy mutable metadata before
  yielding. Returned dictionaries cannot mutate the snapshot. This is not a
  durable quote, frozen tariff, payment authorization, recovery selector or
  public graph interface. Core still prices and reserves through the existing
  live-credit authority. `video_quotes.py` now separately persists bounded
  immutable private quotes; planning alone is still not quotation authority.
- `video_quotes.py` owns `grid_video_quotes`, not reservations or the outbox.
  Five-minute quotes freeze canonical owner/request key, normalized intent,
  source-byte commitments, selected seed/graph/units, undiscounted price-book
  snapshot, canonical Base USDC recipient/network and one derived nonce.
  New binding-v3 snapshots freeze the USDC EIP-712 name/version/chain/token
  domain plus upfront flow/EIP-3009 method. Old v1/v2 remain readable/replayable
  without upgrade, repricing or replacement nonces; only v3 permits fresh
  signing. Prior v2 attempts retain historical proof and observation-only replay.
  Upgrade every quote/staging/proof/refund/financial reader before v3 writes;
  retain those readers/history on rollback, never downconvert existing rows.
  Reads verify the commitment and indexed fields; replay never recompiles or
  reprices. PostgreSQL globally serializes inserts and retained-row caps while
  account locks fence merges; no decoder/storage/network call runs under those
  locks. Cheap capacity preflight precedes native decoding. Preparation is
  additionally bounded to two owners per process and one per local owner; this
  is not a cross-replica decoder lease. Expired rows remain readable and count
  toward the pilot's caps; no deletion/reused identity or payment acceptance
  exists. No quote can enqueue, upload, consume quota, debit, pay or refund.
  Apply `0046` before using these optional routes and retain quote history on
  rollback. Shared receipt ownership is a separate `0048` candidate, not quote
  authority. HTTP acceptance, live payment proof, funded admission and real
  refunds remain gates.
- `video_quote_staging.py` owns private `0049` quote-input persistence, not
  public admission. It verifies the original plan, commits exact private keys
  before storage I/O, and retains those keys on expired-lease retry. Conditional
  S3 puts and bounded streamed readback prove stored size/hash before one fenced
  ready commit. Two local slots remain owned until a cancelled storage thread
  finishes. Recovery refreshes exact private GET URLs without decoding,
  recompiling, repricing or uploading again. No signatures, credits, payment
  sender or outbox are written. First attempt permission now requires matching
  ready state, but no HTTP sender exists. Candidate `0050` now closes expired,
  attempt-free staging irreversibly after lease/grace, then deletes only its
  recorded keys. No recorded payment attempt is eligible, even unresolved or
  reviewed state. Failed validation rotates without granting storage authority.
  ACKs are revisited for late in-flight writes; the provider lifecycle remains
  required. Repeated cancellation retains staging/cleanup guards until their
  owned storage threads finish. The independent default-off startup loop drains
  only unpaid inputs after quote-creation rollback, with count/type-only logs.
  Current optional staging queries require `0050`. Retention transfer to funded
  jobs and live R2 qualification remain gates. The SQLite/PostgreSQL staging, quote,
  attempt and shared-receipt matrix passes 234 cases with nine explicit skips;
  synthetic S3 and real botocore request-model checks are not live storage proof.
  The local full suite passes 3,293 tests with 37 skips and 139 existing warnings;
  it predates the final test-only merge-retry correction, separately qualified
  by 14 real PostgreSQL credit tests. Fresh exact-commit CI remains pending.
- `video_eip3009.py` is an offline, admission-time exact EIP-3009 binding
  helper, not a payment endpoint. It builds unsigned SDK-compatible typed data
  including the mandatory JSON-RPC `EIP712Domain` type definitions,
  and checks canonical 65-byte low-S signatures against one owner-loaded v3
  quote's amount/payer/recipient/nonce/domain/window. It rejects self-payments,
  compact/wrapped signatures, unbound SDK nonces and expired quotes. Core never
  signs or takes private keys; the bound result contains no signature or
  funded state. No database, RPC, replay claim, balance/code check, facilitator,
  upload or dispatch occurs. Live signer classification/token-domain checks,
  routed outer-envelope validation and durable financial admission remain required.
  Do not use its current-window check to abandon recovery of an earlier payment
  attempt after expiry. No real-wallet prompting or published client exists.
  Separate MIT SDK candidates now supply dependency-free policy/preparation and
  explicit signer callbacks. Their actual preview path blocks signing; private
  Core/SQL qualification uses future-enabled copies and unfunded signatures,
  not an enabled HTTP payment rail or released package.
- `media.compile_dispatch` is the non-I/O governed compiler; the existing
  async `prepare_dispatch` wrapper additionally checks compatible workers.
  It freezes the same governed graph/execution units for
  HTTP waiting and async ingress; `bind_dispatch_recipe` attaches that exact
  snapshot without another recipe resolution. Native source validation,
  capability admission are shared through `videos.validate_video` without
  uploads/quota. The waiting `videos.prepare_video` wrapper stages those inputs;
  async ingress stages the already compiled snapshot without recipe re-resolution.
- `video_payment_proofs.py` owns non-routable historical exact-USDC evidence.
  Two distinct operator-selected HTTPS destinations must agree on one direct
  canonical-token v/r/s `transferWithAuthorization`, actual recovered signature,
  exact Transfer and AuthorizationUsed logs, payment-block code/domain/decimals/
  consumed-nonce state, and a re-read canonical confirmation anchor. Every
  state call uses a required-canonical block-hash selector. Payer and relayer
  need not match; any payer code/delegation at that block rejects this EOA lane.
  Safe/finalized and depth are explicit policy inputs, never latest fallbacks.
  Expired quotes remain inspectable; unavailable, pending or inconsistent proof
  never means no payment or permission to retry. Two process-local slots, five-
  second reads, a thirty-second whole-proof deadline, 512-KiB replies, strict
  duplicate-free JSON-RPC and no redirects bound the transport. Caller-owned
  authenticated/TLS-verifying clients remain a future adapter contract; distinct
  hostnames are not operator-independence or cryptographic chain proof. The
  separate attempt journal calls it, but no request/loop, sender or dispatch
  does. The pure verifier writes no claims or money. Wrapped/batched calls,
  receive-authorization, bytes overloads and calldata suffixes are unsupported,
  not silently credited. Live compatibility and atomic funded admission remain
  gates; journal/shared-claim fixture tests do not prove real settlement.
  `locate_authorization` additionally correlates one original transaction from
  an operator-selected confirmed range of at most 1024 blocks. Both sources
  must agree on exact token/payer/nonce logs and re-read range/event anchors.
  It shares the bounded transport/proof slots; a miss is pending, never proof
  of unspent authorization or permission to resend. The result is correlation
  only, not a payment proof. Automatic range selection/scheduling remains absent.
- `video_payment_attempts.py` owns optional `0047` pre-send/reconciliation state,
  not a payment endpoint. First permission requires `0049` verified input
  staging; an old attempt can only recover, never gain another send permission.
  Account/quote locks serialize one initial valid
  authorization winner; created-time/deadline checks run after locks and again
  before commit. It retains a canonical payload hash rather than the spending
  signature. Replays after expiry/rollback grant no send permission. Payer and
  safe/finalized/depth policy are immutable, as is the recorded transaction.
  Owner-family recovery preserves the quote's original commitment; missing/
  foreign state is hidden and mutated bindings fail closed. Network proof runs
  outside SQL locks, then rechecks owner/quote/transaction before one committed
  proof. Repeated proven reads retain the original anchors with no network.
  Unavailable/pending proof leaves state unchanged; rejected proof is manual
  review, not no-payment/refund. Commit-reply loss recovers the original row.
  The separate `0048` shared receipt claim now commits with new proven evidence.
  Claim conflict retains that proof as `receipt_claim_conflict` manual review,
  not a payable/refundable duplicate. Previously proven rows recover unchanged
  and are not silently backfilled into ownership; future admission must check
  its actual shared claim. There is no facilitator, automatic watcher,
  signature-retention/retry, funded hold/outbox, credit or payout write. Private input persistence
  is separately owned by the staging service, not the attempt journal.
  `proven` evidence alone cannot admit work. `recover_lost_transaction` uses
  the bounded nonce-log lookup only for an original unknown `settling` attempt,
  proves the entire transfer before immutable report, then uses ordinary
  reconciliation/shared ownership. Known/proven transactions bypass the scan.
  Network reads hold no SQL locks; report/proof commit-reply failures recover
  the same attempt without a send permission or economic movement. No runtime
  route/loop invokes this journal and normal quote views remain payment-disabled.
- `payment_receipt_claims.py` is the `0048` cross-rail ownership writer. One
  chain/asset/transaction belongs to one deposit/text/video economic reference;
  the same reference cannot adopt another receipt or change purpose. Different
  assets in one transaction retain separate value. PostgreSQL serializes the
  key and SQL uniqueness guards owner races; nested insert conflicts leave the
  caller transaction usable. SQLite explicitly begins the actual transaction
  before savepoints, preventing rollback-orphan claims in legacy driver mode.
  Deposits commit claim/receipt/credit together; independently proved text
  x402 commits claim/settled state together. ETH conversion claims its actual
  USDC token, not native input. Historical books are checked without backfill;
  old conversions missing token provenance reject ambiguous reassignment.
  Bounded canonical AuthorizationUsed witnesses exclude any other rail from
  a matching original video attempt even when no transaction hash is recorded.
  Video proof/claim commit together but do not grant funded admission. Failed
  ownership preserves proof for manual review without automatic credit/refund.
  No RPC, sender, public route, signature or model/worker authority exists here.
  All deposit/text-x402 replicas and offline reconcilers require migration-first
  synchronized upgrade; old writers can bypass claims. Retain claim-aware code
  and history on rollback. Future funded admission must call the ownership gate,
  not infer ownership from an old `proven` label.
- `probe.py` is a retired fail-closed compatibility shim, not a sampler.
  Its old `GRID_PROBE_*` variables cannot dispatch work. Use the existing
  assignment-bound validator or manager-bound setup paths for no-DEN checks;
  paid audit work requires its separate durable budget authority.
- `generation_admission.py` restricts new public generation by exact configured
  path before a billing reservation or dispatch. Media gates live in the shared
  submission path so chat media, SDKs, Gallery, and direct APIs cannot bypass
  them. Source-image, image-batch, and video-timeline capabilities additionally
  require their base image/video path. This is not a pricing or reward exemption;
  internal validator/setup probes retain their separate non-economic contracts.
  Typed video requests with multiple image pins also require `video-timeline`.
  The video router repeats admission before source uploads or quota consumption.
  `audio-to-video`, `video-to-video` and `appearance-to-video` are default-dark subpaths; all
  require `video`, and motion-guided video also requires `audio-to-video`.
  Source-bearing dispatch requires one worker with `recipe-media-sources-v1`
  plus every other requested feature. Appearance additionally requires
  `recipe-appearance-reference-v1` on that same worker. Released workers do not
  advertise the appearance feature.
  `validate_rollout` rejects global charging with an omitted admission list
  before startup touches dependencies. Dark/allowlisted deployment can retain
  defaults; an explicit empty list is a valid closed rollout. This guard forces
  selection, not a claim that the selected paths have been verified.
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
  Recipe-name/checkpoint-name differences pass that audit only with a retained
  Core-dispatched recipe root matching the exact reviewed recipe, requested
  model, job type, and routed checkpoint. Pricing aliases are not evidence.
  Missing historical roots remain findings; never backfill economic records
  merely to make a canary pass. This verifies routing intent, not execution
  fidelity or worker-reported output integrity.
  `treasury_health.py` (default-off Base ETH/AIPG balance warnings through the
  billing monitor; explicit public wallet/token and raw-unit thresholds, no
  signer or payment calls),
  `reward_health.py` (default-off last-complete-UTC-hour warning for reward
  eligibility without full purchased backing; no economic writes),
  `validator_audit_budgets.py` (default-dark compensated-audit budget,
  terminal, and ledger-aware expiry lifecycle; the ordinary worker terminal
  imports it, but no scheduler can create audit work yet),
  `validator_compensation_preview.py` (pure offline allocation simulation over
  unverified reviewer snapshots, with integer caps and assignment/operator-group
  deduplication; no economic authority, database writes, recipient or sender),
  `validator_compensation.py` (explicitly approved, private PostgreSQL pilot
  contracts and finalized allocations; independently rechecks signed completed
  text tasks, shares caps by reviewed control group and claims work globally
  once. No runtime hook, payout queue, recipient selection or sender),
  `validator_compensation_recipients.py` (private, preview-first, immutable
  recipient consent for a positive finalized allocation: node EIP-191 proof,
  recipient EOA/Base EIP-1271 proof and exact maintainer approval digest;
  no account-wallet fallback, visibility-link authority or payment sender),
  `validator_compensation_operator.py` (default-off private pilot/status views
  and allocation-specific wallet/node signature collection; exact current
  account association authorizes access, not payment. Node-row serialization,
  post-lock identity/expiry checks and RPC outside locks protect pending proof.
  Only the private export command feeds existing maintainer recipient review;
  no public bind, sender, campaign creation or credit movement),
  `holdings.py` (cached on-chain AIPG balance + Chainlink ETH/USD),
  `deposits.py` (atomic Base funding receipts from verified account wallets
  plus USDC, bounded AIPG, and conversion-gated ETH claims),
  `x402_payments.py` (default-off accountless
  Base USDC authorization and settlement receipts), `model_registry.py`
  (ModelVault sync).
- `funding_lineage.py` is the offline migration-0042 opening reconciler.
  Preview replays deposit-bound history, debits, refunds and paired merges;
  an exact hash and write-excluding locks are required to append zero-spendable
  opening entries. Never infer funding from a reason label or overwrite old
  ledger rows. New deposits explicitly carry funded provenance, debits consume
  it first, refunds restore only its unused part, and merges preserve it.
  `billing_health` checks funded and spendable caches against their ledger sums
  in one snapshot. This is reward-backing metadata, not another user balance.
  See `docs/architecture/FUNDED_CREDIT_LINEAGE.md` for the mandatory drain,
  migration, opening and all-writer rollout order. No production activation yet.
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
- **Worker setup canary:** `worker_canaries.py` runs a randomized,
  hard-targeted text round trip or a governed image/video/audio render for the
  exact manager-bound rig. Media checks require the expected presigned object
  and managed-worker receipt, then remove the temporary output. The canary is
  five-minute gated per worker, accepts no caller prompt, model, modality, or
  target, and writes no credit reservation, ledger row, den, strike, validator evidence, or
  quality claim.
  Text setup permits at most 512 completion tokens so reasoning backends can
  reach their answer; only the bounded visible answer is compared. A truncated
  mismatching result reports `output_budget_exhausted`, never a quality verdict.
  Challenges identify their label as public test data and delimit it before
  the final instruction so backend-added control suffixes are not part of the
  label. Do not strip such suffixes from answers to manufacture a pass.
  Compare UTF-8 bytes so
  non-ASCII responses fail as mismatches instead of crashing the scorer.
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
  length-style finish, and a documented cross-tokenizer tolerance.
  `text.token_limit.v2` / `token.limit.v2` permits one final proper prefix of
  the committed repeated marker at that cutoff, after at least two complete
  identical markers. Original output (including the fragment) remains counted
  and evidence-hashed. V1 scoring is unchanged for historical replay and open
  assignments. New work prefers v2 only when explicitly advertised, without
  doubling the token-limit lane; v1 capability remains usable for old groups.
  Worker order must not determine the family. Multi-model workers rotate toward
  the least recently covered advertised model. Within that model, new groups
  select an eligible capability never issued before, then the least recently
  issued capability, with cryptographically random ties. Count attempts of every
  outcome so a failing or unfilled lane cannot monopolize the budget. This is
  worker/model-scoped retained-history scheduling, not proof of fresh finalized
  independent evidence or hidden-template security; pruning resets old coverage.
  Keep the existing context/scorer gates, open-group reuse, per-model cooldown,
  and PostgreSQL worker lock. A validator already assigned to
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
  never silently promoted to Core-verified fact. Scorecard rates count votes,
  not independent trials. Per-row sampling metadata counts retained assignment,
  group and registered-validator bindings without exposing their identities;
  independent sample count and confidence intervals remain unknown. Probe age
  uses only retained, completed Core assignments, never attestation receipt or
  validator-supplied timestamps. Missing/future probe times remain explicit.
  Independent-operator counts
  remain zero until externally reviewed; registration count is not independence
  proof. `validator_operators.py` owns the review state: an opaque control group,
  at least 72 hours of qualification, rate-limited heartbeat coverage, an
  expiring maintainer review, at least one completed probe plus authoritative
  attestation created during that qualification window, and preview-first
  compare-and-swap transitions bound to the exact account and signing wallet.
  After migration `0035`, supported heartbeats additionally fill a bounded ring
  of unique server-timed five-minute buckets. Once 72 hours are actually observed,
  qualification coverage uses that recent window rather than averaging all old
  outages forever. Existing lifetime timestamps/counters remain unchanged and
  provide the coverage basis during warmup. Recent-basis reviews and readiness
  monitoring require completed work and authoritative evidence in that same
  recent window. The review digest binds the ring, and heartbeats serialize with
  reviews under a row lock. This never self-verifies an operator or moves money.
  A registration on the frozen cohort release automatically begins a
  non-economic observation window and rate-limited heartbeat sampling; an
  existing unreviewed registration begins on its first supported-version
  heartbeat. Wrong-version heartbeats do not count, and Core never backfills
  missing historical samples. Candidate review begins only for an active,
  fresh-heartbeat registration on the frozen cohort release. It preserves an
  unreviewed node's observed window while attaching the opaque control group;
  this does not itself prove independence or grant authority. Evidence created
  before the observation window cannot satisfy the gate, and review references
  are bounded opaque tokens rather than operator identity or private notes.
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
  version must receive an upgrade action before cohort-review guidance. A fresh,
  supported candidate whose time and coverage gates are complete is directed to
  maintainer review, not told to wait indefinitely. That read-only guidance
  preserves candidate status and does not prove accepted work, operator control,
  or independent eligibility; the existing review transition checks those gates.
  Wallets,
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
  frozen-baseline version drift, fresh baseline registrations waiting for
  operator review, and duplicate reviewed control groups. Waiting-for-review
  alerts are counts-only intake prompts; they do not assert independence or
  start qualification. Its
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
  validity. V8 text batches have no shared execution: only their completed,
  fully bound assignments support a finalized group whose shared probe status
  remains `not_started`. This exception is exact-policy/text-only; media and
  legacy shared probes still require completed group execution. New run defaults
  freeze the configured baseline and sampling interval; explicit mismatches and
  changes to an existing frozen contract fail closed.
  Shadow runs, observations, outcomes, Core-derived capacity samples,
  and bounded errors are private replay records only; routing, worker health,
  credits, settlement, payouts, rewards, bonds, strikes, and slashing must
  neither import this module nor query its tables. The flag defaults off, the
  CLI is read-only, and a completed run cannot promote itself. Missing expected
  sample slots and successful ledger jobs absent from the exact route capture
  count against report coverage. A run's full implementation commit must match
  Core's proven immutable runtime commit at creation, lifecycle, append, and
  report boundaries; an unprovable or changed release fails closed. Review also
  requires at least one real successful ledger job, zero observer errors, and
  zero unmatched captured-success commitments. `route_commitments.py`
  domain-separates a
  stable per-job HMAC from a delivery-specific route HMAC; reports intersect
  unique successful job commitments with the ledger, so wrong-job captures and
  duplicate retries cannot satisfy coverage. `route_events.py` is the neutral,
  non-awaited producer: after actual dispatch it commits both private references,
  reduces the job to bounded metadata before scheduling background work, uses a
  two-second single-flight cache of minimal worker fields to snapshot compatible
  replicas, caps snapshots at 256 candidates and 128,000 UTF-8 bytes, and writes
  no prompt, output, account, wallet, worker name, or validator identity. Because
  production dispatch is worker-pull, this is explicitly a post-dispatch sample
  of connected compatible replicas, not the exact candidate set considered by a
  centralized scheduler; it may include busy or prefetched workers. The policy,
  event, collector, and report must agree on the fixed candidate basis, and the
  report must disclose that its counterfactual is same-model replica preference.
  `validator_shadow_collector.py` is the only outbox consumer allowed to import
  `validator_shadow`; it uses a Redis lease, retries transient faults, records
  outcomes/capacity, and has no routing or economic output. Invalid or
  contradictory events must record a bounded durable observer error before
  acknowledgement/deletion. An unavailable error writer leaves them pending.
  Attribution uses an existing observation, payload time, then Redis insertion
  time, never retry time; replaying old events cannot contaminate a later run.
  Error rows record rejection attempts, not unique-event counts; a crash after
  error commit can append another error on retry but cannot produce a clean
  report. Raw event contents and exception messages are never error records.
  The Redis outbox has
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
  `validator_text_fidelity.py` owns a separate default-off text behavioral
  fingerprint contract. Core sends one sealed temperature-zero request to a
  candidate and one or two explicitly configured trusted references serving
  the same explicitly calibrated model, normalizes only the first bounded top-logprob
  distribution at the worker WebSocket boundary, and freezes the witness set
  once per quorum group. One reference can produce positive consistency
  evidence only; a negative outlier verdict requires two references to agree.
  Missing logprobs, unavailable references, malformed distributions, or
  reference disagreement are inconclusive. Worker-supplied logprobs are not
  cryptographic proof of model identity, and this lane has no routing, reward,
  strike, payout, or slashing effect.
  `validator_responses.py` owns the bounded native Responses observation reader
  and stream accumulator. The internal `_run_targeted_text_stage` can explicitly
  select streaming `openai-responses` for qualification (at most 256 output
  tokens, 300 seconds); public assignment selectors still use the existing chat
  contract. Core dispatches these probes before ordinary raw settlement and
  commits only bounded observations to its Redis replay buffer. Per-event and
  whole-stream limits bound memory; missing probabilities remain explicit gaps.
  Duplicate JSON keys at any depth are unusable, never last-value-wins.
  Message item identifiers must be valid UTF-8 within 128 bytes, matching the
  independent reader. Invalid metadata yields unavailable evidence, not an
  available envelope that the validator cannot consume.
  The envelope retains native probabilities, byte sequences, delta/sequence/item
  indices and visible-prefix hashes, not hidden reasoning or a proven full model
  context. It is incompatible with the chat first-distribution scorer by design.
  No public Responses fidelity policy, runtime attestation adapter, calibrated
  comparison, or live deployment is established by this transport work.
  Tests cover unavailable/partial observations, terminal duplication, malformed
  input, bounded streams, economic isolation, and a real disposable Redis
  queue/collector/replay round trip. `test_validator_responses_redis.py` skips its
  isolated Redis tests if `redis-server` is absent; the separate optional
  `VALIDATOR_RESPONSES_CAPTURE` test accepts a private recorded worker capture.
  A recorded replay is not a live end-to-end validator attestation.
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

- `media.DEFAULT_IMAGE_MODEL` is the exact dispatch name `FLUX.2 Klein 4B FP8`,
  matching the curated text-to-image recipe and its positive price. This is not
  a fallback selector or permission to bypass generation-path admission.

- `identities.account_ownership` reads only canonical/retired IDs from proved
  account merges, bounds the family to 128 and rejects a root changed during
  the read. It never moves money or changes ownership. Historical ledger IDs
  remain intact; consumers may use the authenticated result for data recovery.

- An explicit invalid `GRID_CHARGING_MODE` rejects startup and authorization;
  never interpret a typo as `off` and silently admit unbilled work. Only an
  absent mode uses the documented legacy boolean fallback.

- One queue: `job_queue.py`. Retry handoffs check the original pending delivery
  and append its replacement before acknowledging it in one Redis Lua script.
  A failed append leaves the original pending; repeat handoffs are nonterminal
  no-ops, never a reason to refund a job already handed to another consumer.
  Original stream fields preserve progress, targeting and retry counters.
  Affinity exhaustion keeps the claim for local execution. Generation retries
  use a separate message-carried counter (including any legacy Redis counter
  during upgrade), capped at two for compatible-worker failures. Mismatch
  bounces with no compatible live peer retain their separate cap. Stale recovery preserves both counters;
  it is not itself a backend failure or a new generation attempt.
  Compatibility routing transfers the original pending delivery with XCLAIM,
  guarded by its current consumer and the peer's exact fresh registry snapshot.
  The same worker must have the model, modality, API format and every feature;
  a server-owned `stream-inbox-v1` marker excludes older Core receivers. The
  per-worker inbox stores only delivery IDs plus one-use nonces, bounded by the
  stream cap and twice the wait-window TTL. Changed-owner/old notifications
  cannot dispatch or refund. Pending-load ordering favors a less-loaded peer;
  busy compatible capacity does not consume the absence-search bounce budget.
  Queue age survives mismatch/affinity/stale handoffs; only a genuine capped
  generation retry starts a new capacity wait. Redis rechecks the original
  pending owner and deadline immediately before dispatch after local prefetch;
  expiry closes only that delivery, and superseded readers do nothing.
  Routed waits are bounded to five minutes for text and fifteen for media.
  Expired delivery closes only that
  queue item and must use the existing unstarted-only async refund guard.
  The outbox, execution lease and stale-hold deadlines remain independent.
  There is no model-fidelity claim or new worker binary requirement.
  `tests/test_job_queue_redis.py` uses isolated Unix-socket Redis to prove append
  errors, duplicate races, metadata retention, caps and child-process death
  immediately before/after handoff. Lua errors do not roll back writes; retain
  the append-before-ack ordering and validate before writing. These tests do
  not prove Redis persistence loss, exactly-once GPU execution, stream trimming
  under overload, full Uvicorn restart, or SQL/queue distributed transactions.
  Paid settlement remains guarded separately by its durable reservation.
  `routers/tests/test_worker_dispatch_routing.py` combines real disposable Redis
  with SQLite/PostgreSQL holds for busy-peer delivery without another payload
  or debit, joint capability/target/protocol checks, notification replay,
  registry races, expiry and no-refund-after-execution. Queue subprocess tests
  additionally kill the coordinator around the claim transfer. These are not
  a native-GPU full-film, purchased-credit or production rollout canary.
- Money paths must stay idempotent and tested; value-moving credit ledger writes
  require non-null refs and must not overdraft under concurrency.
- Validator compensation is separate from worker den and audit-execution
  budgets. The manual preview/apply command freezes a seven-day contract and
  requires explicit integer amounts. Candidate validation ceilings are 100000
  AIPG total and 25000 per operator, matching the revised September 13 approval;
  these are not defaults, live activation or changes to existing contracts.
  The command freezes
  reviewed beneficiaries before work, then commits allocations and unique work
  claims together after the receipt grace. It pays neither majority agreement
  nor accusations: only independently reverified, correctly scored generated
  text tasks count. Failed-worker evidence can be valid work. Reference/fidelity
  experiments remain excluded. Allocation records are not sent payments; no
  ordinary worker sender or live validator handler may consume them.
- Candidate budget-linked supplements use `create --parent-campaign-id` to add
  separately reviewed omitted operators prospectively without changing a frozen
  parent. One to ten distinct members are allowed only for a supplement; normal
  campaigns retain the three-member minimum. Parent maximum allocation plus all
  sibling budgets cannot exceed the original ceiling. Parent/sibling control
  groups, accounts, signers and node IDs cannot overlap. Supplements share the
  parent end/release/asset/policy, cannot nest, and cannot extend caps or backdate
  work. Preview/apply and finalization revalidate the linkage under the existing
  PostgreSQL transaction discipline. This is not deployed or activated yet.
- Recipient consent commits the exact campaign, allocation, account, node,
  Base token, integer amount, destination and at-most-24-hour signing window.
  Both parties sign the same domain-separated message; no private key enters
  Core or its CLI. Apply serializes with allocation writes and locks current
  node/account identity, rejects retired accounts and signer rotation, and
  rechecks expiry after verification. The one immutable row per allocation
  makes identical replay safe even after expiry; conflicting consent fails.
  A separately authenticated recipient does not establish operator independence
  or prevent a compromised node from proposing an attacker-owned destination.
  The maintainer must review that destination through the known operator channel.
  This is an administrative backend, not a shipped operator wallet UI or sender.
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
  `tests/test_paid_validator_audit_postgres.py` exercises the worker-ledger/audit
  terminal against a disposable PostgreSQL with one private schema per test.
  It observes blocked backend transactions before releasing a job lock, checks
  twenty duplicate completions and release/success races, and terminates only
  its own transaction backend before commit to prove both halves roll back.
  These tests neither send validator compensation nor replace migration/HTTP
  integration coverage. Their cleanup drops only the test's generated schema.
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
- `authorize_media` checks an optional strict `max_cost_micro_usd` against the
  discounted total before service exposure, promotional/daily allowance or SQL
  debits. It rejects over-budget work as `cost_limit`; the image route exposes
  this as a 402 before dispatch. Dry-run remains zero-charge without activating
  billing. A quote is an estimate, never the ceiling's enforcement authority.
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
- `media_results.py` validates a private, maximum-64-KiB media result envelope.
  The ordinary worker terminal persists it in the reservation's held-to-settled
  transaction, alongside the completion ledger insert. A receipt-write failure
  commits neither success nor payout. Duplicate/late terminals cannot replace
  the winner's result. The envelope holds server-issued output URLs/keys and
  bounded receipt metadata, never prompts, source images or credentials.
  `media_client_ref` captures the caller's bounded progress token at reserve
  time. It is correlation only, not dispatch idempotency or authorization.
  Read-only recovery scopes both selectors to the canonical account family;
  ambiguous references fail instead of returning an arbitrary job.
  Unreserved preview/legacy work has no recoverable reservation. Old settled
  rows without results return `closed_without_result`, not fabricated output
  or a promise of refund. This does not extend R2 object retention, make
  Gallery's pending store durable, or authorize automatically resubmitting work.
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
  Runtime authorization revalidates stored policy: direct services require
  positive integer per-request and daily caps, with per-request no larger than
  daily. Missing, zero, negative, or malformed caps fail closed even for legacy
  records. Delegated user accounts may omit app-wide caps, but any configured
  cap must be valid; their own durable credit reservation remains mandatory.
- `GRID_CHARGING_ALL_MODEL_SERVICES` is a default-empty JSON list of exact
  direct-service IDs allowed to charge across models in allowlist mode. Both
  positive server-owned ceilings and `inference.service_submit` are required
  to admit work. Cohort selection depends on identity/scope, not cap validity:
  missing or malformed caps must reach the fail-closed authorization check,
  never turn a selected service into uncharged preview traffic;
  delegated users and ordinary cohorts retain the model restriction. Global
  `off` still wins. The authenticated credit summary exposes a versioned
  `service_budget` with actual caps and `all_models_charged`; this reports
  policy, not remaining capacity. Requests without an applicable effective rate
  still reject before dispatch.
- Price coverage is modality-specific. A model entry with only a video rate is
  unpriced for image/text, and positive quotes round up to one micro-USD rather
  than silently becoming free.
- Price book `2026-09-17-a` supplies unknown nonempty text model names with a
  standard USD0.075 input/USD0.30 output per million tokens. Explicit entries
  and aliases win, including zero-rate/media entries that remain denied for
  text. Unresolved auto selectors have no quote. `get_price` remains an explicit
  lookup; text quotes and reservation snapshots use `get_text_price` together.
  `/v1/pricing` publishes `price_book.default_text`; model discovery/status
  exposes effective rates and `source=model|default`. No availability,
  registration, verified model identity, reward multiplier or payout authority
  is granted by this customer tariff. Existing held rates never change.
- The book retains the explicit text-only launch peg for
  `qwen38-flash-next-125b-nvfp4`: USD0.075 input and USD0.30 output per million
  tokens. It is not a benchmark-backed competitor comparison or a wildcard
  price for other Qwen models. Adding a rate does not bypass credit admission,
  prove model availability, alter worker rewards, or update existing holds.
- New monetary holds obey `off | allowlist | on`; an existing durable hold must
  still settle or refund after the operator disables new charging.
- Charging allowlists select delegated/user work by canonical account. Service
  IDs select only direct service principals carrying
  `inference.service_submit`; never let a frontend service allowlist charge all
  of its delegated users.
- Operator alerts are best-effort and data-minimized. Never include prompts,
  outputs, email addresses, credentials, signatures, raw exceptions, or
  unredacted identity values in alert fields.
- `credits.billing_health` reconciles purchased balances against ledger totals
  per account in one SQL statement, preserving a single PostgreSQL snapshot.
  Opposite account discrepancies must not cancel out; missing cache rows are
  included. Only aggregate totals and mismatch counts leave the monitor.
- Treasury monitoring verifies Base chain ID and reads ETH/AIPG at one block.
  RPC failures report unknown balances, never zero or cached healthy values.
  It runs even with payout scheduling stopped and never refills or pays. Its
  configured token must match the reviewed AIPG payout token before activation;
  this warning is not an independently verified on-chain settlement proof.
- Text reservations snapshot input/output rates and holder discount at reserve
  time. Never reprice an in-flight job from the current price book.
- `ledger.py` writes one completion event per job. Settlement and stats depend on
  `grid_ledger`; do not revive orphan den tables for new v2 payouts.
- `chat_output.py` owns assembled chat output counts and commitments. Plain
  text-only receipts retain SHA-256(text), or NULL for no output. Reasoning or
  tool output uses canonical JSON with `schema=aipg.chat-output.v2`, `content`,
  `reasoning_content`, and the ordered `tool_calls` list. Counts include text,
  reasoning, function names and argument strings separately using the existing
  Grid tokenizer proxy; call IDs/indexes are not billable. Old visible-only
  worker signatures cannot authenticate this richer receipt and remain unsigned
  until workers adopt its exact commitment. No routing/reward authority is added,
  and historical commitments/charges remain unchanged.
- On-chain reads only via sync loops, cached; never per-request.
- Never copy a payout private key to a worker. Core resolves the payout wallet
  from the API-key account, then verifies its signed delegation to the worker's
  local signer. Registration nonces are one-use and fail closed if Redis is down.
- Changing an account's reward destination does not revoke an already enrolled
  worker identity. Reconnect may use only the same account/name's retained
  verified delegation wallet, signer, certificate ID and expiry. Both signatures,
  capability binding, expiry and nonce freshness still verify. New identities
  require the current payout wallet. `capabilities.delegation_wallet` preserves
  the signing authority separately from the current payment destination;
  legacy verified rows bootstrap it from their stored registration wallet.
  Rotate/revoke worker credentials deliberately; a payout preference is not an
  identity revocation control. No payout history or frozen plan is rewritten.
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
- Ordinary light chat/code and the default light tier prefer `qwen3-27b`.
  Model scoring ranks only within the first nonempty eligible curated tier;
  never flatten tiers or fall back to an arbitrary connected model. Operator
  pins remain subject to the caller's eligibility list. Missing curated
  capacity raises `NoEligibleModel`, not a fabricated model identity.
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

- Cohort version eligibility accepts the frozen baseline plus at most seven exact
  reviewed upgrade versions. The singular legacy setting and plural JSON array
  are mutually exclusive. Python and SQL must agree, including malformed
  prefixes. This preserves existing qualification clocks during a rolling upgrade;
  it neither grants independence review nor activates authority. Malformed text
  fidelity reference IDs must produce a contract error, never a TypeError.

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

- `tests/test_video_quotes.py` exercises immutable quotation on SQLite and
  disposable PostgreSQL: native synthetic source validation, price/seed replay,
  owner/merge isolation, retained-cap races, corruption and migration parity.
  No facilitator, payment, production admission or GPU quality is proved.
- `tests/test_video_eip3009.py` performs real offline ECDSA and locked x402 SDK
  typed-data/digest compatibility on both Base networks, source-commitment and
  changed-domain/payer/nonce/amount/expiry controls, unsupported signatures,
  immutable views and legacy refusal. Synthetic keys never hold funds. This is
  not a live facilitator/contract, published adapter or payment canary.
- `tests/test_video_payment_proofs.py` exercises actual HTTPX request/envelope
  handling with fixture transports on both Base domains, real synthetic ECDSA
  and the official SDK/Web3 ABI. It covers historical recovery after expiry,
  nonce/amount/domain/event/calldata mismatch, payer/relayer distinction, provider
  disagreement, anchored code/delegation, moving heads/reorgs, duplicate/oversized
  replies, redirects, read/whole deadlines and live local-capacity cancellation.
  It performs no SQL/network-chain/payment/admission and does not prove that a
  selected live RPC/facilitator supports this deliberately restricted shape.
- `tests/test_video_payment_attempts.py` uses real SQLite and disposable
  PostgreSQL state with synthetic unfunded signatures and fixture HTTP RPC.
  It proves commit-before-send permission, no retained capability, alias/foreign
  isolation, immutable reports/proofs, late recovery, pre/post-commit reply loss,
  changed intent/policy rejection, and zero credit/hold/job/ledger effects.
  Twenty-five PostgreSQL duplicate callers yield one winner; an observed real
  quote-lock waiter rechecks expiry after release. DDL/schema parity and empty-
  only downgrade are exercised on both databases. Shared ownership now has its
  own matrix below; these original lifecycle cases are not
  facilitator execution, process-SIGKILL, actual payment or funded admission proof.
- `tests/test_payment_receipt_claims.py` verifies both directions across the
  three rails, real deposit/text/video writers, shared/owner purpose conflicts,
  rollback of failed credit writes, conversion-backed asset identity, legacy
  exclusion without backfill, retained conflict proof, and lost-reply nonce
  protection. Twenty-five competing PostgreSQL rail identities yield one claim.
  SQLite savepoint rollback and both DDL/downgrade paths are explicit tests.
  Signatures/HTTP RPC remain synthetic fixtures; no actual payment, production
  mixed-version rollout, complete funded outbox or refund is qualified.

- `tests/test_ltx25_appearance.py` checks the separate fixed Ingredients profile,
  nonanimated/unrotated PNG decoding, default-dark admission, committed roles/
  controls, immutable guide chains and refusal of the unpriced live tariff.
  Optional `MEDIA_VIDEO_WORKER_SOURCE` points to an explicitly reviewed media
  worker checkout with a test `.venv`; its real adapter consumes the Core plan
  in an env-file-disabled child, verifies synthetic bytes and binds renamed
  inputs through mocked HTTP for audio and audio/video. No GPU, live storage,
  wallet, quality or identity proof is claimed. The async router test separately
  exercises both appearance/audio and appearance/audio/video with real scoped
  keys, independently decoded MP4 sources, one hold, exact two-/three-key
  retention and unchanged-ID rollback recovery on SQLite/disposable PostgreSQL,
  with a test-only price and private-upload stub.

- `tests/test_video_output.py` uses synthetic S3/body fixtures to prove
  source-conditional copy, post-copy worker overwrite isolation, streaming
  bounds/closure, digest/header rejection, private-key cleanup, deadlines,
  capacity and uncertain transport. `test_media_job_journal.py` adds SQLite/PG
  terminal controls for canonical result recovery, invalid refund, uncertain
  held state and a stale connection's no-payout/no-publication behavior.
  The process-crash fixture stubs frozen storage explicitly. None of these
  tests is the joint native-worker/GPU/R2 canary or a video-fidelity verdict.

- `tests/test_media_job_journal.py` additionally exercises original-worker/model/
  feature binding, private lease fencing, 25 competing PostgreSQL resumptions,
  refund/resume races, stale detach/renew/terminal rejection and 0043-to-0044
  schema parity. Mocked worker dispatch proves the fresh `resume:true` envelope
  has one original hold/result. The separate router process test kills real Core
  and destroys its private Redis queue; worker output/storage remain synthetic.
  These do not establish joint GPU/R2 recovery or public release qualification.

- `tests/test_ltx25_billing.py` verifies candidate video duration/graph holds
  before dispatch and the ordinary worker terminal's commit-before-DONE/ACK.
  SQLite and isolated PostgreSQL schemas cover exact settlement, result-write
  rollback, timeout recovery, duplicate protection and no payout after release;
  PostgreSQL additionally observes real competing terminal locks and reserve
  overdraft races. Prices/storage/transport/free pockets are synthetic, not a
  live funding, R2, GPU, wallet or price-activation proof.
  The shot-image cases additionally pin an increased reserve-time price against
  the caller ceiling (no dispatch or money movement), plus a successful exact
  ceiling hold with one charge and recoverable result under duplicate completion.
- `tests/test_billing_monitor.py` exercises the actual background loop with
  synthetic health and delivery dependencies: drift and aging are distinct,
  failures are redacted without skipping treasury/reward checks, the next
  iteration recovers, and cancellation is not an incident. This is not a
  production fault injection or Discord acknowledgment proof.
- `tests/test_consumer_activation.py` uses isolated PostgreSQL schemas to pin
  global charging through actual stored legacy-empty-scope/session/scoped key
  authentication and durable text/image/video/audio authorization. Quota-exempt
  flags and unmatched allowlists cannot bypass a paid hold or insufficient-credit
  rejection. Zero free/promo availability and discounts are fixtures; this is not
  a live OAuth, frontend, dispatch, or GPU qualification test.
  Its delegated-user matrix also proves that Google-only and wallet-linked
  account fixtures use their own credit through all four first-party service
  identities, never the funded application's balance. Both service-key-plus-
  token and native user-token transport verify real Grid signatures; upstream
  Google/SIWE proof is outside this test.
- `pytest grid_api/services/` - covers `job_queue`, `den`, `quota` (+ settlement subtree).
- Credit/account merge races may deliberately fail closed with the exact
  canonical-account-change retry signal before money writes. Tests must retry
  the original durable ref, not assume every concurrent call succeeds or
  swallow arbitrary exceptions. The controlled pre-lock merge case proves zero
  initial credit rows and exactly one canonical-account credit after retry.
- PostgreSQL lock-observation tests must refresh `pg_stat_activity` snapshots
  inside a long-lived observer transaction. Prime the snapshot before starting
  the contender and call `pg_stat_clear_snapshot()` before each poll; otherwise
  a new blocked connection can remain invisible and produce a false timeout.
  Still require actual `pg_blocking_pids` evidence before changing tested state.
- `tests/test_validator_compensation_handoff.py` is an opt-in cross-repo
  PostgreSQL/Core/Console/node check. Set disposable `VALIDATORS_TEST_DB_URL`,
  reviewed `VALIDATOR_NODE_SOURCE`, and env-file-free built
  `VALIDATOR_CONSOLE_SOURCE`. It performs real SIWE/Auth.js login, scoped Core
  calls, both exact consent signatures and post-commit response-loss recovery;
  confirmation creates no recipient binding, transfer, credit or worker payout.
  Earning records/independence and Redis nonce transport are synthetic fixtures.
  Each test owns one temporary PG schema and its loopback child processes.
  This does not prove native packaging, Google/wallet-extension UI, Base RPC,
  production rollout, independent operators, or a paid pilot. It skips explicitly
  when the cross-repo sources or disposable DB are unavailable.

## Child DOX Index

- [p2p/AGENTS.md](p2p/AGENTS.md) - default-off P2P decentralization prototype.
- [settlement/AGENTS.md](settlement/AGENTS.md) - Merkle settlement + IPFS + aggregation.
- `tests/` - service unit tests (job_queue, den, quota).
