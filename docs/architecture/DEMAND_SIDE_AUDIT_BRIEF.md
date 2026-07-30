# Demand-Side Economics & Universal Credits — Audit Brief

**Audience:** independent auditor. **Status:** part built (shipped *dark*), part
proposed. **Money is involved**, so this brief leads with the threat model and the
invariants we need you to break.

Companion docs: `GRID_ECONOMICS.md` (full design + thesis),
`PROOF_OF_QUALITY.md` (validator-measured model quality). This brief is the
review-oriented synthesis: what exists, what's proposed, and where it can go
wrong.

---

## 0. TL;DR for the auditor

We are building a **universal credit system** where the **grid is the single
economic authority** and all front-ends (developer console, chat = an Onyx fork,
gallery, third-party apps, agents) are **thin clients**: they authenticate a user
to a **grid account** and call the grid; the grid prices, meters, debits, and
enforces limits. One USD balance per account, spendable everywhere; funded by
many rails (Stripe, USDC/ETH/AIPG on Base, x402).

**Charging is currently OFF** (`GRID_CHARGING_MODE=off`): the metering path runs
in **dry-run** — it computes and logs what it *would* charge but never debits or
blocks. `allowlist` is the mandatory first live stage; `on` is the broad
rollout. The old `GRID_CHARGING_ENABLED` boolean is only a compatibility
fallback.

---

## GO-LIVE BLOCKER CHECKLIST (must be complete before `GRID_CHARGING_MODE=on`)

The independent review (2026-06) confirmed the brief asks the right questions and
that several risks are **already real in code** (they only bite once charging is
on). These are hard gates, not suggestions:

- [x] **B1 (DONE — core reserve path landed b8d4ca2; second pass
  hardened the leaks; fifth pass `b02ada2c` added FREE-FIRST: authorize_request/
  authorize_media draw the daily free allowance before paid, split durable in
  grid_reservations.free_micro, settlement restores free-to-free / refunds
  paid-to-paid, gated on GRID_FREE_SPENDABLE_LIVE — a free-only user with zero
  paid balance is no longer 402'd, 47/47 tests) — Prepaid enforcement.** Reserve/authorize *before* dispatch;
  return **402 before queueing** on insufficient funds; reconcile/refund after
  actual usage. Second-pass fixes: settlement bills on **grid-counted** tokens,
  never worker-reported `usage` (a silent/lying worker can't zero the bill);
  `max_tokens=null` no longer under-reserves.
  Third pass — **durable reservation lifecycle** (this branch): a reserve writes a
  `grid_reservations` 'held' row (Alembic 0004) and the **worker-WS handler is now
  the sole settler** — it reaches a terminal state for EVERY job (success /
  client-error / worker-fault / dispatch give-up) regardless of whether the client
  stayed connected, and flips held→settled **exactly once** (the conditional UPDATE
  is the guard), reconciling against its own grid-counted completion. The HTTP
  collectors no longer settle (dry-run observe + display only), so a disconnect can
  neither strand nor double-settle.
  Fourth pass — **lifecycle extended to ALL job types + crash safety net**: the raw
  passthrough formats (`/responses`,`/messages`) and media (image/video) now reserve
  atomically (`record_reservation`) and settle in the worker-WS terminal too —
  passthrough via `settle_job` on a grid-counted output, media via `settle_exact`
  (exact reserve stands) / `release_job` on failure. A **periodic sweeper**
  (`sweep_stale_reservations`, `_reservation_sweeper` in main.py) releases any 'held'
  row older than `RESERVATION_STALE_SECONDS` (default 1h) — the safety net for a
  crash between reserve and terminal.
  Fifth pass — **atomic terminal + ledger-aware sweeper** (closes the terminal-success
  edges): the worker-payout ledger row and the demand settlement now commit in ONE
  transaction (`credits.record_and_settle` + `ledger.record_completion_in_session`),
  so a crash between them can't leave a paid worker with a refundable hold. The
  sweeper is **ledger-aware**: a stale 'held' row WITH a completion row is settled
  (charged), not refunded; only rows with no completion are released. The disconnect
  path no longer both errors AND requeues — requeue and terminal-error are mutually
  exclusive (only a dead-lettered requeue errors + releases), so a retried job can't
  be refunded-then-completed-for-free. Also fixed `grid_ledger.id` SQLite
  autoincrement (dialect variant) — surfaced because the old `record_completion`
  swallowed the failure.
  Sixth pass — **settlement-gated finalize + no-payout-after-release + migration
  parity**: the queue ack, the worker ack, and the client's DONE are now DEFERRED
  until `record_and_settle` returns a committed state, across text/passthrough/media
  — a settlement 'error' publishes an error and leaves the job UNACKED for
  stale-reclaim instead of becoming free inference + unpaid worker
  (`_handle_worker_generation` no longer self-publishes DONE; media/passthrough
  return False to suppress the ack). `record_and_settle` ROLLS BACK the payout row
  when a reservation exists but is no longer held ('stale_no_payout'), so a
  refunded job can't later mint a worker payout. Alembic 0001 genesis brought to
  parity with schema.py (grid_ledger.job_id UNIQUE — the idempotency guard — plus
  duration/ttft). Real Postgres 16 tests now prove overdraft and duplicate-ref
  behavior. Existing reservations remain settleable or refundable after the
  operator disables new charging.
- [~] **B2 (SUBSTANTIALLY DONE).** Account-admin actions
  (change payout wallet, issue/revoke keys) now require a wallet-proven **session
  key** (`api_keys.is_session`, set only by SIWE wallet-login / dashboard-login;
  `issue_key` forces it false so it isn't caller-settable). A leaked inference key
  can no longer redirect earnings or manage keys. API keys now carry capability
  scopes and Core can bootstrap bridge keys limited to `account.read`,
  `inference.submit`, and `identity.assert`. Wallet login now has a strict
  EIP-4361 challenge that binds the selected address, allowlisted frontend
  domain/URI, Base chain id, issue/expiry time, and single-use nonce; generic
  sign-in verification is default-off. **Still TODO:** deploy Core and the
  Console challenge client together, then split the remaining session-level
  account authority into finer billing/worker scopes.
- [x] **B3a (P0 FIXED `cf0cfd08`, 2026-07-08) — Identity-bridge confused deputy.**
  `POST /v1/accounts/session` OR-matched oauth_sub|wallet|email then `.first()`
  (no ORDER BY) and minted a dashboard-session key for the arbitrary winner.
  Because the console forwards an **unverified** OAuth-asserted email, an attacker
  whose provider profile email = a victim's could be handed a session key for the
  VICTIM's account → change payout wallet → redirect earnings. **Fixed:**
  `_session_match` resolves on exactly ONE authoritative identity (oauth_sub >
  wallet > email *iff* sole-identifier AND `email_verified`); email is never a
  supplement to OAuth/SIWE; create attaches email only if unowned (no merge, no
  unique-collision crash). **Regression proof (prod, both throwaway accounts
  cleaned up):** an OAuth login asserting the victim's email received a *different*
  account (victim untouched); unverified-sole-email → 400. Unit + DB tests in
  `services/tests/test_session_bridge.py` (8 passing) reproduce the exact takeover
  shape. *Also hardened same commit:* passthrough body bounds
  (`_passthrough.guard_passthrough_body`, 413 on >200k chars / depth>64 before the
  recursive sanitize; verified live) and worker-cleanup drain of the prefetched
  `local_jobs` (requeue immediately vs stale-reclaim).
- [~] **B3 — Core built, client rollout pending.** All generation routes accept a
  signed `X-Grid-User-Assertion` only from a scoped bridge key. Core verifies
  `iss/sub/provider/aud/iat/exp/nonce`, caps lifetime at 60 seconds, and consumes
  the nonce through fail-closed Redis replay protection before resolving the
  canonical account. Assertions grant inference identity only. Art, Chat, and
  Console must migrate before the legacy internal session bridge is retired.
- [~] **B4 (SUBSTANTIALLY DONE — chat + media metered 89e1b5d; passthrough gated
  this branch) — Universal metering for ALL job types.** One reserve/debit/reconcile
  abstraction for chat **and** image **and** video (incl. chat-routed media), with
  the media `account_id` bug fixed (was passing `user["id"]` not the account UUID).
  The raw passthrough endpoints (`/v1/responses`, `/v1/messages`) are now **metered
  grid-side**: the prompt is counted by flattening the request per-format
  (system/instructions + messages/input + tool defs) and the completion by counting
  the text the grid actually relayed (stream deltas) or assembled (`full_json`) —
  never the worker/backend `usage`. They reserve before dispatch (402 on
  insufficient funds, native error envelope) and reconcile/refund on the terminal
  event or in a `finally` on disconnect, same as chat.
  Price coverage is now modality-specific, production display/recipe names map
  through explicit aliases, omitted video duration bills the selected recipe's
  baked graph default, and ACE-Step uses the approved low-cost launch peg.
  **Remaining before flip:** approve the provisional Qwen/SmolLM/media pegs from
  measured worker economics; the per-format flatten is a tiktoken proxy
  (o200k_base), not each backend's native tokenizer, so counts are approximate.
- [x] **B5 (DONE, b8d4ca2 + launch-hardening follow-up) — Default-deny unpriced
  model/modality pairs in enforce mode.** A renamed model or a model priced only
  for another modality cannot become free. Positive sub-micro quotes round up to
  one ledger unit.
- [x] **B6 (code-guard DONE, b8d4ca2; hard DB constraint → B7) — Idempotency is structural, not caller-discipline.** `ref` **non-null
  required** for value-moving ledger rows (Postgres allows multiple NULLs through
  the unique index); validate in code; tests.
- [x] **B7 (DONE — credit-ledger `ref` NOT NULL landed, alembic `0008` / `229bca16`).**
  `grid_credit_ledger.ref` is now DB-enforced NOT NULL (+ existing UNIQUE), so the
  value-moving idempotency invariant is reproducible in migrations, not just guarded
  in code. Since extended: `0009` payout-preference cols (HOT-auth-path — must run
  before the code that SELECTs them), `0010` grid_revenue, `0011` grid_payout_legs,
  `0012` grid_reservations.free_micro; `0008` made SQLite-safe (batch_alter_table).
  Genesis and follow-up migrations now match `schema.py`. CI creates a clean
  Postgres 16 database, runs `alembic upgrade head`, and requires `alembic
  check` to report no generated operations before the full Grid suite.
- [~] **B8 (SUBSTANTIALLY DONE) — Sybil / free-credit hard rules.** Done:
  canonical provider identity uniqueness, per-key rate limits, a finite welcome
  campaign budget, verified-Google gating for welcome plus daily baseline, and
  holder-only eligibility for wallet-only free value. The
  free CREDIT bucket **fails closed** by design (Redis down → free=0, paid
  covers; `quota.py` request-COUNT fail-open is the documented cheap tradeoff,
  distinct from credit value). **Remaining (product decisions, front-end
  involving):** device/IP abuse scoring and campaign monitoring before broad
  promotional rollout.
- [~] **B9 (MOSTLY DONE — reserve/refund/idempotency/unpriced covered; real-Postgres
  concurrency landed `9f40ce76` (overdraft race 25v5 → exactly 5 win, dup-ref 12x →
  once); free-first durable suite `b02ada2c` 47/47; Stripe/deposit integration tests
  still pending) — Money-invariant tests.** Duplicate-ref idempotent, null-ref
  rejected, concurrent-debit can't overdraft, insufficient blocks **before**
  dispatch, stream reserve/refund, media-job charging, unpriced blocked in
  enforce mode.

### 2026-07 demand-launch hardening (code complete; dark-deployed)

- Positive purchased-credit accounts bypass only the free request-count quota;
  the atomic prepaid reserve remains the authoritative spend gate.
- Bounded service ceilings now reserve by job id, release on failed
  authorization/no-work terminals, and reconcile max text exposure to actual
  spend after the SQL terminal commit.
- Core strict-SIWE tests cover cross-domain rejection, exact-message matching,
  non-burning invalid attempts, and single-use replay rejection. The Console
  client signs the Core-issued message verbatim.
- Charging now has `off | allowlist | on` rollout modes. Account/service cohorts
  can be selected and optionally narrowed to exact model IDs.
- A read-only monitor checks purchased-balance/ledger equality, negative
  balances, invalid reservation splits, and aging holds. Redacted, rate-limited
  Discord alerts cover signup, deposits, reservations, settlement, sweepers,
  invariants, HTTP 500s, and rate limiting.
- Global charging remains off until identity parity, cross-modality billing
  proofs, price approval, funding UX, and the allowlisted canary in
  `deploy/DEMAND_BILLING_RUNBOOK.md` are complete.

#### Immutable dark-deployment record (2026-07-29)

| Surface | Source commit | Production evidence |
| --- | --- | --- |
| Core | `e9b4e00383a0feeecb27fe3325fdbe042e0465d7` | `/home/aipg/current` resolves to `/home/aipg/releases/grid-core-e9b4e003`; the running process reports `GRID_CHARGING_ENABLED=0` and `GRID_CHARGING_MODE=off`. Public `/health`, `/docs`, and `/openapi.json` return `200`, while the retired `/v2/status/heartbeat` returns `410`. |
| Console | `3cb0816a83220039395a8038bee7bdc250f770fa` | Vercel production deployment `dpl_FW8Poyw5hyEVMoiaiSFrDj1YMfB1` is Ready and owns `console.aipowergrid.io`. Clean-room install, lint, formatting, production build, TypeScript, and production dependency audit passed; token refresh now fails closed instead of moving a live Console session to a different canonical account. The existing signed-in session retained the same `$0.0049` balance after deployment. |
| Chat | `e50687907d760f715ca1fb5cfa0a6a2e1a3921aa` | Backend, background, and web containers use matching `grid-e5068790` images from `/home/aipg/releases/aipg-chat-e50687907d`; public API health and the signed-in footer both report `e5068790`. |
| Art | `5c9bc5c4286a0813a802843a720ec88ad400d060` | The active release resolves to `/opt/aipg-gallery-releases/gallery-5c9bc5c4`; 77 frontend tests, Go tests, `go vet`, the production-config frontend and Go builds, and the production dependency audit passed. Public Studio, Director, and model routes return `200`; unauthenticated credit/job calls return `401`; retired Art audio routes return `404`. Director now displays only Core-owned balance/quote data, links `402` recovery to Console funding, and preserves server-observed Core receipt IDs for first frames and segment renders. Every protected Grid call still fails closed unless Core's exchanged canonical `account_id` matches the signed Gallery session account. |
| Music | `0139a7f3215cd86af07ad6e49e17924852409ec8` | The active release resolves to `/opt/aipg-music-releases/music-0139a7f3`; lint, typecheck, build, auth smoke, production dependency audit, host-header rejection, unauthenticated generation rejection, worker status, and the signed-in balance UI all passed. Quote and generation routes now fail closed unless Core's delegated canonical `account_id` matches the Music session account. The earlier charging-off generation produced Grid job `b8b1fd0e-523a-457b-ba52-f2a4c21fce2a`, displayed its receipt in the UI, wrote one audio completion row, wrote no reservation or credit movement, and left the purchased balance unchanged. |

This table records source provenance, not authorization to enable charging.
Core remains the sole charging-mode authority; frontend deployments cannot
turn billing on.

**Remaining launch order:** prove one Google/wallet-linked canonical account
and balance across Console, Chat, Art, Music, and direct API use → prove
reserve/settle/release invariants for every supported modality → run one funded
account/service/model allowlisted canary → reconcile all receipts and ledgers →
hold the canary for 24 hours → expand cohorts gradually.

The most security-sensitive remaining integration is frontend identity
delegation: Core must verify global Google/SIWE proof itself, while service
principals may delegate only their own namespaced application subjects.

---

## 1. What is BUILT today (release candidate, still dark)

All in `grid-core/grid_api`; production deployment state must be checked against
the immutable release SHA rather than inferred from this document.

- **Credit ledger** (`services/credits.py`, `v2/schema.py`):
  - `grid_credits(account_id PK, balance_micro BIGINT, updated)` — balance cache.
  - `grid_credit_ledger(id, account_id, delta_micro, reason, ref UNIQUE, model,
    created)` — append-only truth.
  - Unit: integer **micro-USD** (USD × 1e6).
  - `credit()` / `debit()` are **idempotent on `ref`** (unique constraint →
    IntegrityError → treated as "already applied"). `debit()` is
    **overdraft-safe + race-safe** via a conditional `UPDATE … WHERE balance >=
    amount` (rowcount 0 ⇒ insufficient, ledger insert rolled back).
- **Pricing** (`services/pricing.py`): USD-native, per model and modality;
  `quote_text/image/video/audio/3d` returns integer micro-USD. Some launch pegs
  remain provisional pending measured worker economics.
- **Split knobs** (`services/economics.py`): protocol/sentinel/worker split (bps),
  worker USDC/AIPG payout split, AIPG-payment bonus, buyback cap — all integer
  bps, splits sum to the whole (no dust). Currently config-of-record, not yet
  consumed by payout.
- **Request-path metering**: all text and media submission paths call
  `authorize_request` or `authorize_media` before dispatch. Selected requests
  write durable holds; non-selected requests remain dry-run.
- **Accounts/identity**: canonical identities, strict Core-issued SIWE
  challenges, Google exchange, scoped per-account keys, and bounded service
  principals. The internal-token session endpoint is retired by default.

**Built ship-dark / migrating:** durable promotional grants, reduced daily free
credit, three-pocket reserve/reconcile, canonical identities and merge aliases,
and the signed frontend bridge. Client migration and shadow-accounting parity are
still required before enabling free/promo spend. **Still proposed:** Stripe,
x402, chat conversion UX, and developer revenue-share.

---

## 2. The identity keystone — and its threat model

Global identity authority belongs to Core. Google tokens are verified against
an allowlisted service audience, and wallet login signs an exact Core-issued
EIP-4361 message with a single-use nonce. Both resolve to a canonical Grid
account and produce short-lived native user tokens.

First-party backends authenticate with a scoped service key. They may exchange
only a namespaced application subject (for example `gallery:<local-id>`) and are
bounded by per-request and daily monetary ceilings. A service key cannot assert
an arbitrary global Google subject or wallet and cannot manage the delegated
user's account.

**Threat model:**

- A stolen service key can spend only within its configured ceilings, but it can
  impersonate that service's local subjects. Rotate it and audit service events.
- A frontend must not send raw email, wallet, or account IDs as billing
  authority. Core accepts only its verified proof/token formats.
- Linking service, Google, and wallet identities must require proof of both
  sides and must conserve balances through canonical account merges.
- Long-lived service credentials stay server-side. Browsers receive only
  short-lived user tokens and never the service key.

---

## 3. Universal credit model (proposed)

- **Tiers** (config-driven): anonymous (small session/IP allowance) → registered
  (a **free-credit grant** on signup) → **Pass ($10/mo → monthly credit grant)**
  → pay-as-you-go. One grid balance, spendable across chat/gallery/API.
- **Enforcement** is grid-side in the request path: out of credit / over limit →
  **HTTP 402 + reason**; the front-end renders an upsell. Free-tier daily quota
  already exists (Redis counter) and is the model for anon/free limits.
- **Funding rails → credit the balance** (independent adapters):
  - **Stripe** (subscription + top-ups) → webhook → `credits.credit(ref=event.id)`.
  - **USDC/ETH/AIPG on Base** → deposit watcher → credit (USDC 1:1; ETH/cbBTC
    swap→USDC; AIPG at peg, never swapped).
  - **x402** → agent pay-per-call (the per-request meter *is* the price).

---

## 4. Money-path invariants to attack (please try to break these)

1. **Idempotency, everywhere.** Every value-moving event carries a unique `ref`:
   chat charge = `job_id`; Stripe credit = event id; crypto deposit = tx hash.
   Re-delivery / retry / replay must never double-apply. *Verify the unique
   constraint is the actual enforcement, not app-level checks.*
2. **Overdraft & races.** Concurrent debits must never drive balance negative
   (conditional UPDATE). Can two in-flight completions both pass on a balance that
   only covers one?
3. **Attribution integrity.** (a) Can a user be billed for another's usage? (b)
   Can usage *escape* metering (a completion that returns content but never
   charges)? Note the dry-run helper swallows errors so billing never breaks a
   response — confirm that, once live, a charge *failure* can't silently grant
   free usage beyond intended.
4. **Trusted-header abuse** (§2) — the headline item.
5. **Free-credit farming / sybil.** Anonymous allowance + per-signup free grant
   invite abuse (new accounts/sessions for free credits). What stops it? (email
   verification? device/IP heuristics? grant only on first funded action?)
6. **Crypto deposit watcher** (when built): chain-reorg safety, confirmation
   depth, no double-credit on tx replay/duplicate logs, correct decimals
   (USDC 6, ETH/AIPG 18), and **AIPG price manipulation** — the AIPG/USDC pool is
   a **thin Uniswap v4 pool** (~$1k depth), so naive spot pricing is trivially
   manipulable; we plan TWAP, but review the window + sandwich resistance.
7. **Stripe webhook** (when built): signature verification, event replay,
   idempotency on event id, and handling of refunds/chargebacks/failed renewals
   (claw back credits? go negative? freeze?).
8. **Rounding / FX.** All integer micro-USD; swaps introduce slippage. Confirm no
   rounding path lets value be created or destroyed across credit↔debit↔payout.
9. **Dry-run → live cutover.** Review the `off → allowlist → on` policy, cohort
   matching, kill switch, existing-hold behavior, and required canary evidence.
10. **Supply-side coupling.** Worker payouts (separate settlement docs) are the
    other half; confirm demand-side revenue and supply-side payout can't be
    conflated or double-counted.

---

## 5. Proof of Quality (context, separate spec)

Model quality is *measured*, not trusted: validator nodes run unpredictable,
auto-graded probes (structured/SVG, reasoning, needle-in-haystack incl.
context-length verification, perplexity) mixed into real traffic; score →
routing/tiers; collateralized by the AIPG worker stake + slashing. Relevant to
economics because **pricing/tiers will reference measured quality**, and the
stake/slash is the load-bearing token utility. Full detail in
`PROOF_OF_QUALITY.md`.

---

## 6. Open questions for the auditor

- Are Core-verified Google/SIWE plus bounded, namespaced service delegation
  sufficient for every first-party surface?
- Sybil/abuse controls for **free credits** — minimum bar before going live?
- **Refund/chargeback** policy for Stripe and its credit-clawback semantics.
- **AIPG pricing** on a thin pool — required TWAP window + manipulation bounds,
  or should AIPG deposits be disabled until liquidity deepens?
- Cutover gating — what allowlisted canary evidence is required before
  `GRID_CHARGING_MODE=on`?
- Are current service-client scopes and monetary ceilings narrow enough for each
  frontend's actual authority?

---

## 7. Repo pointers

- `grid_api/services/credits.py`, `pricing.py`, `economics.py`
- `grid_api/v2/schema.py` (`grid_credits`, `grid_credit_ledger`)
- `grid_api/routers/openai.py` (`_meter_charge`), `routers/accounts.py`
- `docs/architecture/GRID_ECONOMICS.md`, `PROOF_OF_QUALITY.md`
- Chat fork: `AIPowerGrid/aipg-chat` branch `server-wip-snapshot`
  (`backend/onyx/llm/aipg/` = the shared-key provider integration to be bridged)
