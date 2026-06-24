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

**Charging is currently OFF** (`GRID_CHARGING_ENABLED=0`): the metering path runs
in **dry-run** — it computes and logs what it *would* charge but never debits or
blocks. Nothing in production moves customer money today. We want this audited
**before** we flip it live.

---

## GO-LIVE BLOCKER CHECKLIST (from the audit — must be ✅ before `GRID_CHARGING_ENABLED=1`)

The independent review (2026-06) confirmed the brief asks the right questions and
that several risks are **already real in code** (they only bite once charging is
on). These are hard gates, not suggestions:

- [~] **B1 (SUBSTANTIALLY DONE — core reserve path landed b8d4ca2; second pass
  hardened the leaks) — Prepaid enforcement.** Reserve/authorize *before* dispatch;
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
  crash between reserve and terminal. **Remaining before flip:** atomicity of the
  text terminal flip+refund is proven on SQLite only (needs a Postgres concurrency
  test); media price peg.
- [ ] **B2 — Scoped API keys.** Add key scopes/classes
  (`inference.submit`, `account.admin`, `billing.manage`, `workers.manage`,
  `identity.assert`). Account/payout/key-mgmt routes require admin scope; a
  bridge key gets `inference.submit` + `identity.assert` only. *(Today: any v2
  key can do everything.)*
- [ ] **B3 — Signed user assertions (not a raw header).** Resolves the doc
  contradiction (see §2). The chat bridge sends a short-lived **signed**
  assertion (`iss/sub/aud/exp/nonce`) from a scoped bridge key; the grid verifies
  the signature. No raw `X-Grid-User` trust.
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
  **Remaining before flip:** peg media prices (currently placeholders); the
  per-format flatten is a tiktoken proxy (o200k_base), not each backend's native
  tokenizer, so counts are approximate — acceptable as a billing proxy, document it.
- [x] **B5 (DONE, b8d4ca2) — Default-deny unpriced models in enforce mode.** Flip
  `BLOCK_UNPRICED` semantics so an unpriced/renamed model can't be free when
  charging is on.
- [x] **B6 (code-guard DONE, b8d4ca2; hard DB constraint → B7) — Idempotency is structural, not caller-discipline.** `ref` **non-null
  required** for value-moving ledger rows (Postgres allows multiple NULLs through
  the unique index); validate in code; tests.
- [ ] **B7 — Migration ↔ schema reconciliation.** Alembic must match `schema.py`
  metadata (`grid_ledger.job_id` UNIQUE, telemetry columns, credit-ledger
  constraints) so ledger invariants are actually enforced + reproducible.
- [ ] **B8 — Sybil / free-credit hard rules.** Wallet/email uniqueness, rate
  limits, friction (CAPTCHA / device-IP), abuse scoring; and quota infra must
  **fail closed (or degrade), not fail open**, on Redis error.
- [~] **B9 (PARTIAL — reserve/refund/idempotency/unpriced covered; Postgres-concurrency + Stripe/deposit tests pending) — Money-invariant tests.** Duplicate-ref idempotent, null-ref
  rejected, concurrent-debit can't overdraft, insufficient blocks **before**
  dispatch, stream reserve/refund, media-job charging, unpriced blocked in
  enforce mode.

**Recommended build order:** B1+B6+B5 (+ tests) → B4 (universal metering) →
B2+B3 (scoped keys + signed bridge identity) → B7 → B8.

The single most security-sensitive new piece is the **identity bridge** for chat
(trusting a caller-supplied user header). Please focus there.

---

## 1. What is BUILT today (shipped dark)

All in `system-core/grid_api`, deployed to prod, gated OFF.

- **Credit ledger** (`services/credits.py`, `v2/schema.py`):
  - `grid_credits(account_id PK, balance_micro BIGINT, updated)` — balance cache.
  - `grid_credit_ledger(id, account_id, delta_micro, reason, ref UNIQUE, model,
    created)` — append-only truth.
  - Unit: integer **micro-USD** (USD × 1e6).
  - `credit()` / `debit()` are **idempotent on `ref`** (unique constraint →
    IntegrityError → treated as "already applied"). `debit()` is
    **overdraft-safe + race-safe** via a conditional `UPDATE … WHERE balance >=
    amount` (rowcount 0 ⇒ insufficient, ledger insert rolled back).
- **Pricing** (`services/pricing.py`): USD-native, "half the cheapest
  competitor", per-model; `quote_text/image/video` → micro-USD.
- **Split knobs** (`services/economics.py`): protocol/sentinel/worker split (bps),
  worker USDC/AIPG payout split, AIPG-payment bonus, buyback cap — all integer
  bps, splits sum to the whole (no dust). Currently config-of-record, not yet
  consumed by payout.
- **Request-path metering** (`routers/openai.py::_meter_charge` →
  `credits.charge_request`): called once per chat completion (stream +
  non-stream). Returns `free | legacy | dry_run | ok | already | insufficient`.
  **In dry-run it logs `would_charge` and returns; never debits/blocks.**
- **Accounts/identity**: `/v1/accounts/session` (internal-token find-or-create +
  per-user key), per-account API keys, `/v1/account`, `/v1/account/workers`.
  Console uses this end-to-end.

**Not yet built / proposed:** funding rails (Stripe, deposit watcher, x402),
tiered free-credit grants + enforcement, the chat identity bridge, the chat
conversion UX, developer revenue-share. These are design only.

---

## 2. The identity keystone — and its threat model (review this hardest)

**Problem.** The chat (Onyx fork) currently calls the grid with **one shared
`AIPG_GRID_API_KEY`** configured as its LLM provider. The grid therefore sees
*all* chat traffic as a single account — per-user metering/credits/limits are
impossible as-is.

**RESOLVED (post-audit): signed user assertions, not a raw header.** The earlier
"trusted `X-Grid-User` header" idea is **superseded** (it also contradicted
`GRID_ECONOMICS.md`, which proposed per-user keys). Adopted model = **B2 + B3**:
the chat authenticates with a **scoped bridge key** (`inference.submit` +
`identity.assert` only) and sends a **short-lived signed assertion**
(`iss/sub/aud/exp/nonce`) identifying the end user; the grid verifies the
signature and meters/attributes to that user. No raw-header trust; the bridge
key cannot mint keys, change payout wallets, or manage workers. The original
header proposal is kept below only as the rejected baseline / threat reference.

**Rejected baseline — trusted-caller + user header.** The chat keeps its shared key
but passes the signed-in user's identity per call (`X-Grid-User: <onyx-user-id>`).
The grid resolves that to a grid account and meters/enforces against it.

**Threat model (the crux):**
- **Spoofing / cross-user billing.** If *any* caller could set `X-Grid-User`,
  they could bill other users or evade their own limits. **Mitigation
  (mandatory):** the header is honored **only** when the request is authenticated
  by a key explicitly flagged as a *trusted front-end* key — never by ordinary
  user/API keys. Ordinary keys ignore the header and bill themselves. **Auditor:
  verify there is no path where an untrusted key's `X-Grid-User` is honored.**
- **Shared-key blast radius.** The chat's shared key becomes a high-value secret
  (it can act on behalf of any user). Requires: secret hygiene, rotation, and
  scoping (trusted-front-end keys should be able to *meter as a user* but not,
  e.g., mint keys or change payout wallets for that user).
- **Honest-but-curious chat.** We trust our own front-end to assert the right
  user. Acceptable for first-party surfaces; **not** a model we'd extend to
  third parties (they get their own per-account keys instead).
- **Alternative considered:** provision a real grid key per chat user and thread
  it through Onyx's LLM call path. Stronger isolation, much more invasive to
  Onyx (one-key-per-provider model). We chose the header approach for first-party
  surfaces; auditor input welcome on whether the isolation tradeoff is acceptable.

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
9. **Dry-run → live cutover.** The flip is a single env flag. What's the rollback
   if pricing/metering is wrong under real traffic? (We've been observing dry-run
   logs against prod traffic first.)
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

- Is the **trusted-header** identity model acceptable for first-party front-ends,
  or do you require per-user keys / signed user assertions even there?
- Sybil/abuse controls for **free credits** — minimum bar before going live?
- **Refund/chargeback** policy for Stripe and its credit-clawback semantics.
- **AIPG pricing** on a thin pool — required TWAP window + manipulation bounds,
  or should AIPG deposits be disabled until liquidity deepens?
- Cutover gating — what evidence from dry-run would you require before
  `GRID_CHARGING_ENABLED=1`?
- Key scoping — should "trusted front-end" be a new key class with a restricted
  capability set (meter-as-user only)?

---

## 7. Repo pointers

- `grid_api/services/credits.py`, `pricing.py`, `economics.py`
- `grid_api/v2/schema.py` (`grid_credits`, `grid_credit_ledger`)
- `grid_api/routers/openai.py` (`_meter_charge`), `routers/accounts.py`
- `docs/architecture/GRID_ECONOMICS.md`, `PROOF_OF_QUALITY.md`
- Chat fork: `AIPowerGrid/aipg-chat` branch `server-wip-snapshot`
  (`backend/onyx/llm/aipg/` = the shared-key provider integration to be bridged)
