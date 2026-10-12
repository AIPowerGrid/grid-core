<!-- SPDX-FileCopyrightText: 2026 AI Power Grid -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Video x402 Build

Status: **private quotation, offline signature-binding and read-only historical
receipt/attempt-journal/shared-ownership candidates implemented; payment collection remains a proposal**.
Reviewed against the candidate async video code and locally locked x402 2.16.0
SDK on October 11, 2026. The quote routes below are independently default-off
and have no payment collection, reservation, dispatch, refund sender or
production authority. Credit-backed video remains the first integration path;
[LTX qualification](LTX_2_5_VIDEO.md) and model-use permission still gate launch.

## Current Boundary

The current `/v1/x402/chat/completions` rail uses `upto` and settles after the
handler returns its complete response. Its middleware buffers that body.
Wrapping `/v1/videos/jobs` in it would let the SQL outbox dispatch before payment
is independently proved. A successful 202 is not a completed video, and a valid
payment signature is not received revenue.

Current owners are `routers/video_jobs.py`, `services/media_jobs.py`,
`services/credits.py`, `services/x402_payments.py`, `services/deposits.py` and
`services/settlement/aggregate.py`. The async journal currently requires an
account and a live credit hold. Existing x402 reconciliation requires a terminal
actual charge. Neither is already a prepaid-video implementation.

`routers/video_quotes.py` and `services/video_quotes.py` now own a separate
immutable `grid_video_quotes` table (migration `0046`). There is no relationship
that grants a quote row a reservation or executable outbox authority. Apply the
migration before using the optional routes, including GET recovery after
rollback. Downgrade refuses to discard any retained row, even an expired quote.
`services/video_eip3009.py` supplies an offline, admission-time verifier only.
No HTTP handler accepts a signature or calls this helper to move money.

## Proposed First Version

Use **exact canonical Base USDC payment for one frozen video job**. Governed
media cost is deterministic; collect that quote, never an arbitrary ceiling.
Keep an existing scoped Grid credential for ownership and GET recovery. This
removes human checkout, not authentication. Accountless recovery and smart-
contract wallet support are later, separately qualified work.

Reuse the official x402 `exact` scheme and facilitator, not a new contract.
Limit the first adapter to canonical USDC's supported EIP-3009 path. Pin and
test the facilitator's exact scheme/network/asset support; existing `upto`
configuration is not proof. The
[official exact scheme](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)
defines payment authorization and settlement. Job binding and the async lifecycle
below are Grid-specific additions, not standard x402 guarantees.

Implemented candidate routes, **private and default-off, not deployed**:

- `POST /v1/x402/videos/quotes`: scoped authenticated request, UUIDv4
  `Idempotency-Key`, bounded video intent and positive cost ceiling.
- `GET /v1/x402/videos/quotes/{quote_id}`: owner-only frozen quote status.
- `GET /v1/x402/videos/quotes`: owner-only lost-reply recovery with the original
  `Idempotency-Key` header.

All three require `inference.submit`, with the existing service/user delegation
rules. New rows return 201, identical POST replay returns 200, changed intent or
ambiguous merged keys return 409, and foreign/missing reads return the same 404.
Responses are `no-store`; they explicitly return `payment_enabled: false` and
do not emit a `PAYMENT-REQUIRED` header. GET and identical replay remain available
after disabling new quotes or expiry; expired identity never means reprice,
extend, replace or mint a new nonce. Recovery uses GET, not another generation.
The additive `binding_schema` names the stored quote-binding revision. New v2
quotes include `payment.eip712_domain`; domainless v1 quotes remain recoverable
with their original commitment/nonce and cannot be upgraded into signing quotes.

**Not implemented:** `POST /v1/x402/videos/quotes/{quote_id}/pay`. The future
route must bind the identical intent and signed exact payment, with no alternate
account, recipient or executable graph. No current route accepts a payment
signature, invokes a facilitator, checks chain receipts or owes a refund.

Quotes never upload inputs, debit pockets or enqueue. Native source validation
now returns private `VideoInputs` without quota/uploads. The non-I/O compiler
creates an immutable `aipg.video-plan.v1` graph/seed/units/source snapshot; async
credit ingress binds uploaded locations to that exact graph rather than
re-resolving it. Source validation still performs compatible-worker/ownership
reads and bounded local decoding. The compiler itself performs none of those
reads, storage, quota, monetary I/O or GPU work. Its in-memory snapshots are
not themselves persisted quotes, frozen tariffs or payment authority. `prepare_video`
still consumes quota/uploads as the legacy waiting wrapper and must not be used
as a quotation API. The separate quote service now freezes and persists the
private plan and undiscounted positive governed tariff, independent of credit
pockets, holder discounts, free allowance or current balance. Stored sources
are byte/role/timing commitments, not source bytes or uploaded objects. The
owner response omits the private full graph, prompt, salt and key hash.

## Private Quote Limits

`VIDEO_X402_QUOTES_ENABLED` defaults false. New quote admission also requires
one to ten distinct allowlisted account UUIDs, a nonzero configured recipient,
and an explicit UTC pilot deadline at most 24 hours ahead when enabled at
startup. Admission rechecks the deadline after source preparation and again
under the SQL commit lock. Existing media/model/source gates still apply.
Enabling this pilot is not public model-use clearance or payment activation.

`VIDEO_X402_QUOTE_NETWORK` defaults to `eip155:84532` (Base Sepolia); the only
other accepted network is `eip155:8453`. Assets are the canonical Circle USDC
contracts, never an operator-supplied bridged token. Each quote expires five
minutes after creation and freezes that exact authorization deadline. The
default operator ceiling is 1,000,000 micro-USD, bounded to 1..1,000,000,000;
the client's positive ceiling must also cover the exact tariff. Neither is a
spend approval while `payment_enabled` remains false.

Retained-row caps default to eight per account family and 256 globally, with
configured bounds of 1..32 and 1..4096 respectively. Expired rows consume the
same cap: saturation rejects new quotes rather than deleting replay identities.
PostgreSQL transaction-scoped advisory serialization plus account locks recheck
capacity and identity before insert. No network or decoding work runs under
those locks. SQLite compatibility tests are not concurrency proof.

Before decoding, a cheap capacity check rejects already saturated pilots.
Preparation admits at most two owners per process and one preparation per
owner, without an unbounded wait queue. MP4 decoding has its own bounded
executor and retains decoder capacity through cancellation. This is **not a
cross-replica decoder lease**. Distributed preparation admission and bounded
retention with durable replay tombstones are required before public-scale quote
admission; neither may silently allow expired keys to become new payments.

## Request And Payment Binding

Commit canonical owner, hashed idempotency key, normalized request hash, source
byte/role/timing commitments, one selected seed, resolved graph/recipe commitment,
execution units, price version, exact micro-USDC amount, recipient, chain/token
and deadline in an immutable quote. New binding-v2 snapshots additionally freeze
the token's EIP-712 name/version/chain/verifying-contract domain. This binding is implemented for private
quotation; the existing request hash alone does not
freeze a generated seed or compiled graph; never resolve either after payment.

Allocate one job UUID and random quote salt. Derive a unique 32-byte authorization
nonce from a versioned canonical quote commitment. Publish its verification
fields; the offline verifier now requires the signed nonce to match.
Changing the job, prompt, source,
price, graph or deadline requires a new quote, never mutation. SQL uniqueness
binds the planned nonce to one quote/job. Unsigned `extra` or `resource` metadata
alone cannot supply this binding. Current nonce uniqueness and commitment
verification alone do not prove a signature, received payment, cross-rail replay
protection or compatibility with an unmodified x402 client.

[EIP-3009](https://eips.ethereum.org/EIPS/eip-3009) signs nonce, value, recipient
and validity, and emits `AuthorizationUsed`. Committing the Grid quote through
that nonce is our proposed adapter, not knowledge of the prompt in the standard.
The locked SDK's default exact client generates its own random nonce. An explicit,
tested opt-in client adapter must choose the quote nonce **before signing**;
editing a signed payload is invalid. Do not claim stock-client interoperability
without that test, or fall back to an unbound payment for unsupported wallets.

### Offline Signature Binding

The helper constructs JSON-ready unsigned `TransferWithAuthorization` data
through the pinned SDK's EIP-712 builder. Core never signs or takes a private
key. Tests use newly generated, unfunded keys and the actual locked SDK signer,
not a mocked recovery function. They prove compatible typed-data hashes and
signatures when the quote nonce/deadline are selected before signing, and reject
the stock client's random nonce or alteration of its already-signed payload.
Separate SDK candidates now implement the opt-in preparation and explicit
signer-callback sequence: Python `2f599e1a` (`0.2.0a2`) and JavaScript `60547a07`
(`0.2.0-preview.2`). They independently implement the public wire contract under
MIT, without copying AGPL runtime code or adding crypto dependencies. This is
**not a package release or an enabled payment rail**.

Both require a trusted authenticated quote plus independently retained expected
model/intent hash/recipient/network/ceiling. They check the nonce commitment,
canonical domain, exact integers and signing window before returning unsigned
JSON-RPC data, including mandatory `EIP712Domain` definitions. Never use a copied
response hash as independent intent verification or claim to prove the hidden
execution plan. Current `payment_enabled:false` blocks signer callbacks.
Explicit future-enabled callbacks sign only, with expiry rechecked afterwards;
private signed capabilities are not exposed by ordinary repr/JSON. No SDK HTTP,
payment sender, fallback, retries or key handling is added.

Private SQLite/PostgreSQL quote HTTP/auth plus both SDKs pass twelve generated/
audio/video and Base/Base-Sepolia protocol cases. Actual preview signing is
refused; only test-local future-enabled copies exercise new unfunded signatures,
private two-phase IPC and Core's real verifier. Every case leaves balance
unchanged with no hold/job/completion. Python's normal suite passes 116 tests,
JS passes 104 and compiled runtime smoke passes two cases; strict typing, build,
format/package inspection and npm audit also pass locally. This does not qualify
a live wallet, funded contract/facilitator, production, GPU or real payment.
The exact SDK candidates also pass GitHub Python 3.9..3.12 and Node 18/20/22/24
runtime/package/security matrices. The combined existing video HTTP/recovery
and new signing qualification passes 68 cases locally on SQLite/PostgreSQL.
Core CI now pins those exact SDK commits and runs both suites; the fresh Core
`0019bfde` revision passes its exact Linux/PostgreSQL 16 CI gate. Later changes
still require their own exact-revision result.

Verification requires a caller-owned, authenticated SQL quote and an expected
payer. It accepts exactly the inner `authorization` and `signature` fields with
canonical string amounts/deadlines, and compares every field against the frozen
quote. Signature recovery uses `eth-account`, the frozen domain and the SDK's
standard types. Only raw 65-byte signatures with v=27/28 and canonical low-S
are accepted. Self-payment, compact signatures and wrapped smart-wallet forms
are refused, without returning the supplied signature or request values.
The quote window is checked before and after cryptographic work.

The result retains no signature or paid/funded state. Its authorization identity
commits chain/token/payer/nonce for the durable attempt journal; **this offline
helper writes no claim**, so repeating its valid verification is not prevented.
It does not check balance, authorization consumption, live token implementation/
domain, payer code/delegation, confirmation, receipts or facilitator support.
These require separately qualified bounded chain reads before financial
admission. Offline key recovery alone does not prove the live account is an EOA.
The future ingress must also validate the outer x402 envelope and owner/intent;
unsigned metadata cannot replace nonce binding. Nothing prompts a real wallet.

This is an admission-time current-window check, not a historical receipt
verifier. An already attempted payment still requires reconciliation after
expiry; the helper's expiry refusal is never proof that funds were not sent or
permission to discard a refund/recovery obligation. No live payment route exists.

### Historical Receipt Candidate

`services/video_payment_proofs.py` now provides a separate read-only verifier,
without an HTTP route or background hook. Its caller must supply an owner-loaded
immutable v2 quote, expected payer, original transaction, two operator-selected
HTTPS RPC destinations and an explicit `safe` or `finalized` confirmation tag
plus a bounded depth. Client TLS/authentication/lifecycle configuration is a
future adapter responsibility, never a caller-supplied payment request field.
Requests use those actual destinations, not merely asserted source labels.

Both sources must prove the expected chain, a successful direct canonical-USDC
`transferWithAuthorization` v/r/s call, exact quote fields, real recovered
signature and exactly one matching Transfer plus AuthorizationUsed event.
Receipt/transaction/log/block identities must match. Token domain separator,
six decimals and consumed authorization state are read at the required-canonical
payment block hash. The payer must have no code/delegation there; a separate
gas-paying relayer is valid. Both sources must agree on the same payment facts
and a re-read confirmation anchor, even if their heads advance between reads.
Distinct RPC hostnames are defense in depth, not proven provider independence
or a cryptographic light-client proof. `safe` is not mislabeled as finalized.

The helper deliberately permits historical inspection after quote expiry. The
transaction must have mined inside the original quote window, but late proof
still needs the future attempt/admission/refund transaction to discharge its
liability. An expired quote never means no transfer. Pending, unavailable or
inconsistent evidence cannot authorize a new payment, render, charge or refund.
The result retains no signature/calldata or funded-job flag. No SQL is written.

HTTP reads have five-second limits and a thirty-second whole-proof deadline,
two process-local active slots, 512-KiB reply bounds, duplicate-free finite JSON,
strict JSON-RPC result identity and no redirects. Payer/contract/receipt errors
never reflect input, endpoint errors or spending capabilities. The caller-owned
HTTP clients must be closed by the future adapter. The initial shape rejects
wrapped/batched transactions, `receiveWithAuthorization`, bytes overloads and
calldata suffixes; facilitator compatibility with this shape remains unproved.
There is no silent fallback that treats a merely matching amount as funding.

The 170-case Base/Base-Sepolia suite uses fixture HTTP transports, actual newly
generated unfunded Ethereum signatures and the locked official SDK's Web3 ABI.
It tests expiry recovery, altered events/calldata/domains/code/anchors, provider
disagreement, malformed/oversized responses, redirect refusal, deadlines and
live local-capacity cancellation. It is not a live token/RPC/facilitator,
purchased-payment, prior-attempt, shared-claim, GPU or on-chain refund proof.

Separate October 11 **live read-only** checks use the new bounded HTTP reader
against [Base's published RPC endpoints](https://docs.base.org/get-started/connect-to-base)
and PublicNode. Each pair agrees on chain ID and the same safe block hash, and
accepts required-canonical block-hash reads of canonical USDC code, the frozen
EIP-712 domain separator and six decimals:

| Network | Common Safe Block | Common Hash |
| --- | --- | --- |
| Base | `0x320de5b` | `0xf1d59507f101b88d99caeaeb6aad5c626f41e2b737e393726cff507ff10b3e3f` |
| Base Sepolia | `0x2dc5d6a` | `0xea3a01e4adb36fdbefb3ef8377ddc79c78052dc79bf04667d7d51a340f3da134` |

The exact pairs are `mainnet.base.org` / `base-rpc.publicnode.com` and
`sepolia.base.org` / `base-sepolia-rpc.publicnode.com`. This verifies sampled
contract-domain and RPC-selector compatibility, not provider independence,
payer code/balance, an unused/consumed nonce, a historical transfer, facilitator
support, economic admission or refund. No wallet, signature, transaction sender,
money movement or production configuration is involved. Future payment proof
must check its own actual payment block; this sample cannot substitute for it.

### Durable Attempt Candidate

Migration `0047` creates empty `grid_video_payment_attempts` with one quotation
FK, unique authorization identity/transaction, immutable payload fingerprint,
payer, confirmation policy and start time, plus coherent nullable receipt/review
state. `services/video_payment_attempts.py` is non-routable; no endpoint or
background loop invokes it. It writes no credit, reservation, executable journal,
completion or payout. The later shared ownership integration is described below.
Normal quote views still return
`payment_enabled:false`. Optional journal use requires the migration; ordinary
quote recovery does not. Downgrade refuses any attempt or unresolved liability.

`begin` locks the owned quote and current account family before verifying the
original signature, requiring `0049` matching ready inputs, and committing
`settling`. Existing attempts without staging only recover their original state,
never acquire new send permission. It checks the explicit admission
deadline/quote window after locks and before commit. Exactly one caller receives
the initial send permission; every exact replay, including after expiry or
admission rollback, receives no permission. Changed payer, payload or confirmation
policy conflicts. Only the canonical payload hash is retained, not a signature
or capability that an automatic retry can spend. No facilitator call exists.

`report` commits one transaction correlation, never a paid state; another hash
cannot replace it. `reconcile` performs the prior receipt verifier outside SQL
locks, checks its block timestamp against the committed attempt start, then
rechecks ownership/quote/transaction before persisting the original proof.
`proven` replay is a read of that same evidence, not another network check or
permission to admit work. It never claims a newer confirmation policy. Pending/
transport errors leave the row unchanged; rejected proof records `manual_review`,
not a claim that no funds moved. Pre-/post-commit reply loss preserves the same
attempt and cannot produce a second send permission.

A crash after `begin` but before the facilitator call can therefore leave an
unsent `settling` attempt. Likewise, a lost facilitator reply may leave the
transaction unknown. Both need the future bounded nonce watcher/confirmed
unspent-authorization closure or reviewed correlation; neither may be reset or
automatically resent. The separate staged-input candidate is described below;
no facilitator or funded admission consumes it yet. New proven evidence commits its shared receipt ownership,
but remains a recoverable liability until funded admission/refund commits; this candidate must
not take real payments merely because its SQL tests pass.

Local SQLite/PostgreSQL 14 plus fixture-HTTP quote/signature/receipt/attempt
verification passes 347 tests with six SQLite variants of PostgreSQL-only
proofs skipped. PostgreSQL variants execute: 25 duplicate `begin` calls yield
one winner and an observed blocked quote-lock contender refuses after expiry.
Generated/audio/video intent, account aliases, altered authorization/policy,
immutable report/proof, zero economic effects and commit/reply uncertainty are
covered. A separate empty PostgreSQL migrates genesis through `0047`, reports
no schema drift and passes its empty-only downgrade/re-upgrade. These are not
actual facilitator calls, process-death injection or received/refunded funds.
The complete local Core suite passes 3,168 tests with 34 skips and 139 warnings
in 432.75 seconds. The two additional skips are SQLite variants of the new
PostgreSQL duplicate/lock-expiry tests, whose PostgreSQL variants pass. Other
optional/SQLite-only skip boundaries remain as described below. New/changed
service and test files pass targeted Ruff; inherited schema-file lint findings
remain outside this scope and are not claimed fixed.

Signed payment headers are temporary spending capabilities, not recovery tokens.
Never put them in URLs, logs, result envelopes or ordinary evidence exports.
Retained authorizations need private access-controlled storage and bounded
expiry. Reads authenticate the owner; neither a quote ID nor a supplied wallet,
payment header or idempotency key is independently an access credential.

## Shared Receipt Candidate

Migration `0048` creates empty `grid_payment_receipt_claims` with unique
chain/actual-asset/transaction and economic-kind/reference identities. Private
bounded purpose metadata is hashed and immutable. The initial unit is one
token/transaction, not an individual log: legacy deposits sum matching token
transfers and text/video x402 cannot split one token/transaction across jobs.
Different token assets in a transaction retain their separate value.

New deposits atomically commit ownership, receipt and purchased/funded credit.
ETH conversion claims actual USDC rather than its native input. Text x402 claims
ownership only with its independently confirmed exact terminal charge; its
configured RPC chain must match the recorded network. A new video proof commits
its ownership in the proof transaction, not a funded hold/outbox. Conflict
retains the observed money proof as `receipt_claim_conflict` manual review;
it cannot cause a second charge/credit or automatic refund.

Canonical bounded AuthorizationUsed witnesses also exclude another rail from
a matching original video attempt when the facilitator reply/transaction hash
was lost. This prevents a deposit or text reconciliation from repurposing that
same spent video nonce. The ordinary recorded-transaction and historical-book
checks remain additional guards. Spent nonce recognition is not itself payment
proof, an unused-nonce oracle, a transaction watcher or dispatch authority.

Existing economic books are read as exclusion evidence without backfill. Old
conversion receipts lacking token provenance conservatively refuse ambiguous
reassignment; new claim records identify the actual token. Previously proven
video rows recover unchanged and acquire no fabricated claim. Future atomic
admission must require the actual matching shared ownership row, not infer it
from an old `proven` label. Claims retain replay identity permanently.

PostgreSQL transaction-scoped key locks and uniqueness enforce receipt/owner
races. Nested insert conflicts preserve the caller transaction for review;
SQLite explicitly starts a physical transaction before its savepoint so outer
rollback cannot orphan a released claim. This behavior is separately tested.
No new RPC, sender, public video API, reservation or outbox is added here.

**Deployment boundary:** quiesce deposit/text-x402 writers and offline runners,
prove exact-release backup/restore, migrate through `0048`, then upgrade every
writer before resuming. Mixed-version writers can bypass claims and are unsafe.
After first use, rollback retains ownership and claim-aware financial writers;
downgrade refuses any stored claim. Applying this migration does not enable
video collection or grant model-use permission. No production changes are made.

## Proposed Payment Flow

### Private Input Staging Candidate

Migration `0049` creates empty `grid_video_quote_staging`, one manifest per
immutable quote. `video_quote_staging.py` verifies the original in-memory plan
against that quote, then commits exact private object keys before uploading.
Neither raw bytes, expiring access URLs nor spending signatures enter the row.
Allocated keys survive partial upload, database reply loss and lease replacement;
no historical source URL is adopted. The quote's retained pilot capacity also
bounds the number of staging records.

Two local slots bound active staging. Recorded-authority S3 operations use
conditional creation and streamed size/SHA-256 readback, with bounded object
sizes, individual network reads and a cooperative whole-storage deadline.
Cancellation retains the slot until its owned thread finishes. An expired or
replaced lease cannot publish ready state. Storage/SQL uncertainty keeps the
allocation incomplete, with no payment permission. Exact ready replay uploads
nothing, and owned recovery refreshes only private GET URLs; graph, source
commitments, selected seed, quote price and request identity remain unchanged.
Account/bucket rebinding or corrupted commitments fail closed.

A first payment-journal `begin` now requires matching ready inputs under the
existing account/quote locks. Earlier attempts lacking staging may still be
observed, not reset or granted another send. There is no payment endpoint,
facilitator, funded hold/outbox, refund, worker dispatch or automatic cleanup
hook. Future admission must transfer the exact input authority to funded-job
retention; unpaid/uncertain allocations need their own safe cleanup policy.
The provider lifecycle remains a backstop, not proof of application cleanup.
Do not enable collection with those gates unfinished.

The combined staging/quote/attempt/shared-receipt matrix passes 234 tests with
nine explicit skips on SQLite and a real disposable PostgreSQL 14 database.
It covers actual conditional-put parameters through botocore's request model,
partial/wrong/truncated storage, source-body closure, preserved keys, pre-/post-
commit reply loss, canceled threads, 25 competing stagers and an observed
quote-lock waiter rejected after expiry. Synthetic storage is not real R2 or
GPU evidence. Scratch Alembic upgrade through `0049` and `alembic check` pass;
The full local Core run passes 3,293 tests, with 37 skips and 139 existing
warnings, in 463.21 seconds. It predates the final test-only credit-merge retry
correction below; a separate 14-case real PostgreSQL credit suite qualifies
that final change. Fresh exact-commit CI and live storage proof remain pending.

### Intended Flow

```text
Agent: scoped key + bounded intent + idempotency key
  -> Core: immutable quote, seed, recipe, exact amount and nonce
  -> Agent: verify intent/ceiling; sign exact USDC authorization
  -> Core: verify binding; stage private inputs and frozen dispatch plan
  -> SQL: commit attempt before contacting facilitator
  -> Facilitator: settle exact payment
  -> Core reconciler: independently prove authorization + confirmed transfer
  -> SQL: funded payment + held reservation + executable job journal, atomically
  -> Existing outbox -> original-worker lease -> verified output
  -> SQL: existing atomic completion/charge/result, or exact refund obligation
  -> Agent: authenticated GET recovery; no new signature or generation POST
```

Chain I/O belongs in the dedicated adapter/reconciler, not worker dispatch or
terminal handlers. Never hold SQL locks over network calls. An attempted payment
must be durable before any side effect. The outbox sees no executable job until
the independently funded transaction commits.

| Payment State | Meaning | Dispatch |
| --- | --- | --- |
| quoted / verified | Quote or valid authorization only | Forbidden |
| settling | Durable attempt; outcome may be unknown | Forbidden |
| reported | Facilitator transaction, not independently proved | Forbidden |
| proven | New original receipt plus shared ownership stored; no hold/outbox | Forbidden |
| funded | Exact confirmed payment committed with held job | Existing outbox only |
| manual_review | Ambiguous payment or inconsistent proof | Forbidden |
| refund_due / refund_pending | Money owed back, refund not proved | Forbidden |
| refunded | Exact outbound refund independently confirmed | Forbidden |

Keep `completed`, `closed_without_result` and `execution_uncertain` as distinct
execution states. Timeout means neither stopped work nor completed refund.
Success spends the fixed quote without a second facilitator capture. Uncertain
renders keep their financial authority until existing lease/stale-close rules
resolve them. Rollback stops new quotes/payments, not funded work, reconciliation,
refund obligations or private-input cleanup.

## Money And Crash Invariants

- No free/promotional/purchased draw for the same prepaid job. Add a versioned
  prepaid source and audit every hold, release, sweep, recovery and reward
  selector. Do not overload the text rail's current `settled_micro` meaning or
  treat facilitator `reported` as funding.
- Independently prove expected chain/token, successful receipt, confirmation
  and block anchor, exact payer/recipient/amount and authorization nonce. Refuse
  ambiguous batched-transfer attribution initially. Amount alone does not prove
  which authorization was consumed.
- Claim authorization/transfer identity once across payment and deposit rails,
  not merely within one table. Existing deposit transaction-sender checks are
  not a substitute for explicit cross-rail ownership. Keep replay tombstones
  after quote cleanup.
- After proof, payment, external hold and job insert commit together. Crash
  between chain success and SQL commit leaves a recoverable liability, not a
  fresh payment request. Lost facilitator replies enter reconciliation; never
  generate a fresh signature/nonce to recover them. Prove unbroadcast failure
  before defining any same-authorization retry policy.
- Payment proved after quote/admission/runtime expiry is owed back if no job
  was admitted. Never extend, reprice or divert it to Grid credit implicitly.
- Definitive failure closes the hold and records an exact USDC refund obligation
  atomically. Late success after closure cannot mint reward. `release_job` or
  restored credit is not an on-chain refund.
- Refund only to the verified payer with immutable amount, durable sender nonce
  and exact receipt protection. Initially use reviewed Safe transactions plus a
  read-only reconciler; autonomous USDC sending is a separate gate. Core gets no
  Safe signer, and the AIPG worker payout CLI is not a USDC refund sender.

## Local Qualification

October 11 candidate verification uses Python 3.13, local PostgreSQL 14 and
FFmpeg/FFprobe. The focused quote/signature suite passes 157 tests; four skips are the
SQLite variants of PostgreSQL-only concurrency/DDL proofs, whose PostgreSQL
variants execute. The independent signature suite passes 68 tests without RPC
or funded keys. The full Grid suite passes 2,942 tests with 32 skips and 139
warnings. Skips include optional private captures, reviewed worker/Console
integration and separately configured shadow Redis checks, alongside the
SQLite variants of PostgreSQL-only tests; they are not claimed as verified.
A separate empty database migrates from genesis through `0046`, and
`alembic check` reports no schema drift. New Python files pass targeted Ruff;
tracked source and full reachable HEAD history pass the existing secret and
infrastructure scan policy without new exclusions.

The preceding quote-only `d3d4e122` passes exact-commit Linux/Python 3.12/
PostgreSQL 16 [required CI](https://github.com/AIPowerGrid/grid-core/actions/runs/38177700720):
2,872 full-suite passes, restored migration/schema parity through `0046`, decoder
and shadow Redis checks, pinned video SDK/Core HTTP qualification and the
Console/node compensation handoff. Those results do not qualify the later
signing-domain/verifier changes. The offline verifier `0022f640` separately passes
[exact-commit Linux/PostgreSQL 16 CI](https://github.com/AIPowerGrid/grid-core/actions/runs/38179418365).
The later RPC domain-type completion and pinned SDK gate `0019bfde` pass the same
local 2,942-test Grid suite and their separate
[exact-commit Linux/PostgreSQL 16 CI](https://github.com/AIPowerGrid/grid-core/actions/runs/38182266267).
The subsequent historical receipt candidate passes a full local Core run of
3,110 tests, with the same 32 skips and 139 warnings, in 439.78 seconds. A final
JSON exponent-overflow guard then passes 238 targeted tests: 170 receipt cases
plus 68 exact-signature cases. The earlier full run predates those final two
overflow cases. Receipt revision `18354a8e` subsequently passes its own
[exact-commit Linux/PostgreSQL 16 CI](https://github.com/AIPowerGrid/grid-core/actions/runs/38184190435).
The `0047` attempt journal `b81fff0a` separately passes its own
[exact-commit Linux/PostgreSQL 16 CI](https://github.com/AIPowerGrid/grid-core/actions/runs/38186145247).
The following `0048` shared-ownership changes still require fresh exact-commit
CI; those prior results do not qualify the new cross-rail transitions.
Receipt revision `ff776aac` completed its required CI with one failure:
`test_credit_racing_merge_is_not_stranded_on_retired_account` assumed both
concurrent calls always succeed, while the unchanged credit helper deliberately
rolls back if the canonical account changes before locking. The new test-only
correction accepts only that exact retry signal, retries the same durable ref
and proves one credit. A controlled PostgreSQL merge between lookup and lock
also proves zero initial credit rows, retired-account zero balance and one
canonical credit after retry. All 14 isolated PostgreSQL credit tests pass.
No financial guard is relaxed and the failed CI is not claimed qualified;
the current staging/guard/retry candidate needs its own complete CI run.
The final shared-ownership candidate passes the full local Grid suite:
3,225 passed, 35 skipped and 139 existing warnings in 461.76 seconds, with
the real disposable PostgreSQL 14 fixture configured. Its 25-way cross-rail
race, rollback and migration cases execute; PostgreSQL-only SQLite variants
and optional external integration checks remain skips, not claimed proof.
An isolated empty PostgreSQL database migrates through `0048` and passes
`alembic check` without drift. Targeted Ruff and `git diff --check` pass.
The receipt tests have no real payment or SQL mutation. Real payment and
native quote-to-funded job qualification remain pending. Neither
local nor CI fixture results change public serving permission.

## Build And Qualification Order

1. Private pure planning and immutable quotes are implemented candidates.
   Local SQLite/PostgreSQL 14 tests cover schema/Alembic parity, source
   validation, expiry/rollback recovery, owner isolation, request conflicts,
   duplicate inserts and retained-capacity races. They forbid quota/uploads,
   reservations and dispatch. Required PostgreSQL 16 CI remains an exact-commit
   gate; fixtures do not prove production or payment compatibility. Public-scale
   preparation and replay-tombstone retention remain separate work.
2. Offline exact signature binding now passes wrong nonce/amount/payer/domain/
   deadline, changed source commitments, legacy-domain refusal and unsupported
   signature controls. The tested opt-in SDK signing sequence is not a released
   client. The SDK callback candidates are implemented/tested, not published.
   Finish outer-envelope integration and qualify
   live account/token/facilitator compatibility before taking payment.
3. Attempt journal, independent chain proof and atomic prepaid hold/outbox.
   The receipt verifier and durable prior-attempt journal are candidates with
   real SQL/fixture transport proofs. Shared cross-rail claims now integrate with
   deposits/text/video, with migration/all-writer rollout and fresh CI gates.
   Private `0049` staged-input persistence is a local, non-routable candidate:
   exact private keys commit before upload, conditional writes and bounded
   readback precede a fenced ready commit, and recovery refreshes locations
   without changing source/graph/seed commitments. Focused SQLite/PostgreSQL
   tests pass as part of the 234-case matrix above using synthetic storage.
   First attempt permission requires ready inputs, but no HTTP sender exists.
   The local full suite and targeted merge-retry tests pass; fresh exact-commit
   CI and real R2 compatibility remain pending;
   cleanup of unpaid inputs and transfer to funded-job retention remain gates.
   The bounded lost-transaction watcher, actual facilitator/payer compatibility
   and atomic funded admission are not implemented yet.
   Prove duplicates, concurrency and cross-rail claims on PostgreSQL 16. Keep
   the existing text x402 contract unchanged.
4. Integrate leased success/failure and reviewed refunds. Kill processes before/
   after facilitator call, proof, SQL commit, dispatch, completion and refund.
   GET recovery must never cause another payment or render.
5. Exact Base Sepolia payment/refund with the selected facilitator and isolated
   real GPU/R2 work. Fixtures do not prove facilitator/token compatibility,
   actual refunds or Mainnet funds.
6. Director/SDK/skill recovery and budget UX; separately approve one capped
   Mainnet payment and a failed-job refund. Public support still requires model
   permission and all video release gates.

Do not present quotation as a shipped payment rail, or the proposed financial
transitions as implemented. No production defaults, funds or running workers
are changed by this candidate.
