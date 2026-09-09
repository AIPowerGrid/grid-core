# grid_api/services/settlement - worker payout rails (live custodial + pass-through + future trustless)

> Three rails live here. (1) **LIVE:** custodial `payouts.py` — hourly systemd
> timer, fixed AIPG budget, treasury hot wallet. (2) **BUILT, DARK:** the
> pass-through multi-asset rail (`revenue.py` + `multiasset.py`) — the DECIDED
> payout model, gated on funded treasury + live charging. (3) **FUTURE, STUB:**
> the trustless on-chain claim rail (`bot.py` Merkle → reportPeriod → claim) —
> RewardPool, DenReporter, and PaymentRouter facets are deployed on the Base
> diamond, but the publisher/claim operation is not live. Never present (3) as
> the current worker payout rail.

## Purpose

Pay workers for metered grid usage. Today: custodial AIPG pro-rata by den.
Next: pro-rata **pass-through** — distribute the actual revenue basket
(USDC/ETH/AIPG) per asset by den, **no conversion** (same basket in, same basket
out; that property is what keeps the grid out of money-transmitter/exchange
territory — see `docs/architecture/PAYOUT_EXECUTOR.md`). Eventually: trustless
Merkle claims on Base.

## Ownership

**Live custodial rail:**
- `payout_periods.py` freezes each closed, post-cutoff UTC hour once, together
  with every allocation, before any signing/broadcast. The reviewed hourly cap
  bounds `--budget`; the canonical hour ID and unique hour slot reject aliases
  and overlapping/custom windows. Replays verify the commitment and stored rows,
  never recompute weights or adopt late arrivals. An empty hour is frozen too.
- `payouts.py` - custodial CLI/timer: fixed AIPG budget pro-rata by den,
  nonce-bound, Transfer-proven, idempotent per (period, account). **Every send
  is OFAC-gated** (`sanctions.screen` before funds move). Its fresh-nonce
  allocator spans worker and validator tables (`grid_payouts`,
  `grid_payout_legs`, `grid_validator_compensation_payments` - one
  treasury account = one nonce space).
  Screening is enforced inside the common `_settle_one` before signing or
  rebroadcast, including accrued and retry entrypoints. Recording proof of an
  already-mined transfer does not require another screen and never sends funds.
  A held attempt uses `manual_review` (fits the existing 16-character status
  column) and retains its existing nonce/hash; screening
  cannot cancel a transaction already in the mempool. Unexpected broadcast
  failures retain the signed transaction hash, never replace it with RPC error
  text. Tests use simulated chain outcomes and actual PostgreSQL persistence.
  Sender entrypoints themselves hold the shared treasury transaction advisory
  lock, not just the CLI. Amount, DEN, non-null recipient and nonce cannot be
  rewritten after recording. Walletless prospective accrual can bind its first
  current account wallet once; pending rows with no nonce survive failure
  before signing/broadcasting. No new public payout status is introduced.
  Transfer units use Decimal arithmetic, and the RPC must identify Base.
- `sanctions.py` - OFAC screening: local denylist (`GRID_SANCTIONS_DENYLIST`,
  authoritative, zero-I/O) + optional Chainalysis oracle
  (`GRID_SANCTIONS_ORACLE`). FAIL-CLOSED: hit → `blocked_sanctions`;
  oracle-configured-but-unreachable → `review_sanctions` (hold, never pay blind).

**Pass-through multi-asset rail (dark):**
- `revenue.py` - per-asset revenue pots (`grid_revenue`, append-only, idempotent
  on ref) + `compute_multiasset_payouts` (PURE: den-share of EACH asset pot,
  conserves every pot) + `worker_pots` (85% worker share per asset via
  `economics.worker_share_bps`). ⚠️ The intake feed (`record_revenue` callers)
  must be EARNED revenue (consumption, deposit-lineage), never raw deposits.
- `multiasset.py` - the per-leg Base sender (`send_period_multiasset`): inherits
  every payouts.py invariant (OFAC gate, nonce-bind-before-broadcast,
  consumed-nonce-unproven → manual_review, escalating replacement fees).
  Proof per kind: ERC-20 = matching Transfer log from THAT token contract;
  **native ETH = the tx itself (to+value) + status-1 receipt** (no Transfer log
  exists for native value). Legs in `grid_payout_legs`, idempotent per
  (period, account, asset). Unsupported pot assets are held loudly, never guessed.
- `assets.py` - payout asset registry (AIPG/USDC erc20 + Base addresses +
  decimals; ETH native; `to_base_units`).

**Future trustless rail (stub):**
- `bot.py` - the settlement bot (orchestrates a settlement run).
- `merkle.py` - cumulative Merkle tree + proof generation.
- `aggregate.py` - roll up per-worker/per-den earnings for a period (input to ALL rails).
  Its read-only `reward_backing_health` compares the same eligible DEN with
  purchased backing in one SQL snapshot. It includes walletless account accrual
  and legacy wallet-only eligibility, excludes unfunded x402, and reports only
  aggregate exposure, never a claim that a payment occurred. `_purchased_den`
  is shared with the existing prospective eligibility rule; monitoring must not
  change its boundary or historical allocations.
- `ipfs.py` - publish the proof set off-chain.
- `tests/` - `test_merkle.py`, `test_ipfs.py`.

**Validator operator compensation (default-off, separate from worker den):**
- `validator_payments.py` binds one reviewed, finalized allocation and signed
  recipient consent to one immutable Base AIPG transaction. A PostgreSQL
  transaction shares the worker payout advisory lock, assigns the nonce and
  commits signed bytes before broadcast. Retries use identical bytes, including
  after uncertain broadcast or output failure; no automatic fee replacement.
  Exact sender/token/recipient/amount Transfer evidence at a finalized canonical
  Base block is required for `sent`. Missing consumed-nonce proof and malformed
  receipts require manual review. The private CLI is not called by a timer.

## Local Contracts

- **Money moves only after OFAC screening.** A screen hit or an unverifiable
  address is a terminal row status, never a send.
- **Proof, not trust:** a payout is `sent` ONLY on a proven receipt (matching
  Transfer log, or tx to+value for native). status==1 alone is NOT proof. A
  consumed nonce that can't be proven becomes `manual_review` — never re-sent,
  never auto-`sent`.
- **One nonce space:** fresh nonces exceed the max bound in all three payout
  tables. Apply migration `0038` before updated worker payout code runs, even
  with validator sending disabled. All treasury-sharing runners must use the
  updated allocator before validator sending is enabled. Lock acquisition
  failures must propagate, never be interpreted as success.
- Validator sending requires explicit `VALIDATOR_COMPENSATION_SEND_ENABLED=1`
  and a separately reviewed exact payment digest. Neither source availability
  nor allocation/recipient consent authorizes funds movement. Never delete
  stored transaction bytes or roll back nonce-aware worker code after a
  validator payment is bound. Protect database backups like signing capability.
- **No conversion in any rail.** The pass-through model distributes the basket
  as received; swaps/fees were deliberately rejected (exchange/MSB exposure).
  Fiat/USDC off-chain legs are the Stripe rail (design: PAYOUT_EXECUTOR.md).
- Settlement input is `grid_ledger` via `aggregate.py`; do not read orphan or
  legacy den tables for v2 worker payouts.
- `WORKER_REWARDS_PAID_ONLY_SINCE` is an optional timezone-aware, prospective
  boundary shared by account, wallet, and diagnostic aggregation. Retain its
  exact value after activation. Before it, legacy DEN is unchanged; at/after
  it, only settled positive purchased-credit work or settled x402 payments
  contributes. Mixed free/promo/paid jobs contribute only the purchased fraction.
  Missing, held, released, unknown-source, and malformed reservations contribute
  zero. Free/promotional usage does not earn unrestricted emissions; compensated
  audit and any future free-work subsidy require separate explicit budgets.
- Custodial emission allocation caps post-boundary SmolLM-family work at 50
  basis points of the requested period budget across ALL accounts (including
  walletless accrual). It preserves a smaller natural share and never
  redistributes clipped allocation. This is an emission cap, not a model
  fidelity claim or a cap on the dark earned-revenue pass-through rail.
  Keep payouts paused until the boundary, budget, hourly scheduling, and live
  reconciliation are reviewed. These functions do not authorize backpay.
- Both custodial allocation branches validate finite nonnegative budgets,
  minimums and account weights before returning payable or accrued rows. Total
  floating-point weight overflow and invalid SmolLM subsets reject the whole
  preview/send, including empty or zero-budget inputs. Valid historical
  arithmetic and the prospective cap are unchanged; this is input validation,
  not a new emission rate or authorization to resume payouts.
- Migration `0041` and reviewed frozen-period code must precede sender
  resumption. New plans require an unchanged paid-only cutoff and a closed
  whole UTC hour after it, capped by `PAYOUT_HOURLY_BUDGET` (208.33 default).
  A changed budget, minimum, cap policy, token or cutoff cannot reprice a plan;
  conflicts fail closed. Automatic accrued/retry selectors only consume rows
  with a verified plan. Historical rows remain owed/reviewable but untouched,
  and need a separate reconciled backpay procedure. Do not restart an old
  sender after plans exist: its recomputation bypasses these contracts.
- Merkle leaf and proof formats are wire contracts with on-chain claim logic.
  Any format change must update tests and known vectors.
- A settlement run must be idempotent: repeated runs must not double-report,
  double-claim, skip closed periods, or pay wallets without ledger support.
- IPFS pinning can fail without aborting a settlement only if the on-chain root
  and local durable proof artifact remain retrievable by ops.
- Reporter/hot wallets are gas-only. Admin/funding wallets must remain hardware
  or multisig controlled and outside process env.

## Work Guidance

- Any change to root construction requires updating `test_merkle.py` and re-deriving a known
  vector. Treat the Merkle format as a wire contract with on-chain claim logic.
- Keep docs honest: if `GO_LIVE.md` describes a command, `bot.py` must actually
  implement that CLI and env names must match.
- Prefer small pure functions for period boundaries, aggregation, snapshot
  serialization, proof generation, and transaction planning so dry-runs can be
  tested without Base RPC.

## Verification

- PostgreSQL: set disposable `PAYOUTS_TEST_DB_URL` and `CREDITS_TEST_DB_URL`,
  then run settlement tests plus multiasset/revenue/sanctions service tests.
  Frozen-period coverage includes late-arrival overpayment, recipient changes,
  atomic rollback, concurrent plan creation and all imported sender locks,
  legacy exclusion, wallet-later accrual and nonempty downgrade refusal.
  The hourly wrapper test copies it and substitutes a recorder for Python;
  no test executes the actual timer against a treasury.
- `pytest grid_api/services/settlement/tests/` (Merkle/IPFS).
- `pytest grid_api/services/tests/test_revenue.py test_multiasset.py test_sanctions.py`
  (pass-through engine, sender routing + proofs, OFAC gate).
- Add bot integration tests before enabling live settlement.

## Child DOX Index

- None - leaf.
