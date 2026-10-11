<!-- SPDX-FileCopyrightText: 2026 AI Power Grid -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Video x402 Build

Status: **private quotation candidate implemented; payment remains a proposal**.
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
and deadline in an immutable quote. This binding is implemented for private
quotation; the existing request hash alone does not
freeze a generated seed or compiled graph; never resolve either after payment.

Allocate one job UUID and random quote salt. Derive a unique 32-byte authorization
nonce from a versioned canonical quote commitment. Publish its verification
fields; the future payment verifier must require the signed nonce to match.
Changing the job, prompt, source,
price, graph or deadline requires a new quote, never mutation. SQL uniqueness
binds the planned nonce to one quote/job. Unsigned `extra` or `resource` metadata
alone cannot supply this binding. Current nonce uniqueness and commitment
verification do not prove a signature, received payment, cross-rail replay
protection or compatibility with an unmodified x402 client.

[EIP-3009](https://eips.ethereum.org/EIPS/eip-3009) signs nonce, value, recipient
and validity, and emits `AuthorizationUsed`. Committing the Grid quote through
that nonce is our proposed adapter, not knowledge of the prompt in the standard.
The locked SDK's default exact client generates its own random nonce. An explicit,
tested opt-in client adapter must choose the quote nonce **before signing**;
editing a signed payload is invalid. Do not claim stock-client interoperability
without that test, or fall back to an unbound payment for unsupported wallets.

Signed payment headers are temporary spending capabilities, not recovery tokens.
Never put them in URLs, logs, result envelopes or ordinary evidence exports.
Retained authorizations need private access-controlled storage and bounded
expiry. Reads authenticate the owner; neither a quote ID nor a supplied wallet,
payment header or idempotency key is independently an access credential.

## Proposed Payment Flow

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
FFmpeg/FFprobe. The focused quote suite passes 87 tests; four skips are the
SQLite variants of PostgreSQL-only concurrency/DDL proofs, whose PostgreSQL
variants execute. The full Grid suite passes 2,872 tests with 32 skips and 139
warnings. Skips include optional private captures, reviewed worker/Console
integration and separately configured shadow Redis checks, alongside the
SQLite variants of PostgreSQL-only tests; they are not claimed as verified.
A separate empty database migrates from genesis through `0046`, and
`alembic check` reports no schema drift. New Python files pass targeted Ruff;
tracked source and full reachable HEAD history pass the existing secret and
infrastructure scan policy without new exclusions. Exact-commit Linux/
PostgreSQL 16 CI remains pending, as do real payment and native quote-to-funded
job qualification. These local results do not change public serving permission.

## Build And Qualification Order

1. Private pure planning and immutable quotes are implemented candidates.
   Local SQLite/PostgreSQL 14 tests cover schema/Alembic parity, source
   validation, expiry/rollback recovery, owner isolation, request conflicts,
   duplicate inserts and retained-capacity races. They forbid quota/uploads,
   reservations and dispatch. Required PostgreSQL 16 CI remains an exact-commit
   gate; fixtures do not prove production or payment compatibility. Public-scale
   preparation and replay-tombstone retention remain separate work.
2. Exact client binding and server verifier: wrong nonce/amount/payer/domain/
   deadline, changed sources/graph and unsupported signing/account types.
3. Attempt journal, independent chain proof and atomic prepaid hold/outbox.
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
