# Post-launch hardening

Status: active work, September 11, 2026. Sole maintainer owns rollout approval.
This document retains the full four-part objective; the billing launch being
complete does not complete these requirements. Production remains `793fe904`
with global charging and the existing hourly AIPG sender. No change below is
production activation evidence.

## 1. Demand-bounded worker rewards

Problem: purchased-backed DEN removes free-work emissions but still lets a tiny
paid job capture a whole 208.33 AIPG hour. A farmer can use multiple identities;
checking that customer and worker wallets differ cannot solve this.

Candidate implementation:

- [x] Read actual settled purchased consumption for work served by each worker
  account. Exclude free/promo portions, missing/ambiguous job reservations,
  invalid amounts, and x402 without a sufficient settled payment.
- [x] Clip each existing DEN allocation to its own consumption allowance,
  retaining the hourly ceiling and SmolLM-family cap. Never redistribute clips.
- [x] Use integer micro-USD and downward-rounded Decimal AIPG, including the
  frozen plan. Commit policy and consumption evidence before signing.
- [x] Keep old plans unchanged; retries never reread current demand. A new
  policy changes only new whole hours. A persisted v2 plan prevents later v1
  plan creation if the configuration is accidentally removed.
- [ ] Finish review, full-suite/CI qualification, and production read-only
  comparative reports before selecting a real valuation and activation hour.
- [ ] Announce the prospective earning change and roll it out with a frozen
  tiny-payment/replay proof. Do not reprice prior rewards or backpay.

For each account, the candidate formula is:

```text
natural_allocation = existing DEN/hour allocation, including SmolLM cap
backed_ceiling_AIPG = purchased_consumption_microUSD * worker_share_bps
                     / (10,000 * price_microUSD_per_AIPG)
allocation = floor_8dp(min(natural_allocation, backed_ceiling_AIPG))
```

The existing minimum payout still applies after clipping. Below-minimum amounts
are not new accrued obligations; this preserves the existing dust policy.
Walletless positive allocations follow the same cap and existing accrual path.

`WORKER_REWARD_DEMAND_POLICIES` is empty by default. Entries have `since`,
`until`, `price_micro_per_aipg`, and `worker_share_bps` (candidate default 8500).
Entries must be contiguous whole-UTC-hour windows of one hour to seven days,
beginning no earlier than the immutable paid-only cutoff. Keep prior entries;
append reviewed new windows. After the last window, new payout hours fail
closed. Old frozen plans still verify against their original retained policy.
No per-inference RPC, swap, or new asset sender is introduced.

The test valuation of 1,000 micro-USD per AIPG is SYNTHETIC, not a proposed
live price. A stale or too-low valuation can still overpay in market terms.
Activation requires an explicit reviewed valuation, funding-epoch compatibility,
expiry/renewal procedure, and downside analysis. The cap is an emission bound,
not proof of model fidelity, independent demand, or impossible arbitrage.
Purchased-pocket accounting also does not establish external-cash provenance
for legacy operator-issued purchased grants: audit those before activation.
If the objective is strict cash-cost self-farming unprofitability, earned-USDC
payment avoids token valuation risk but requires completing and qualifying the
separate earned-revenue rail, not repurposing deposit balances as revenue.

Evidence: `test_demand_rewards.py` tests tiny-demand attacks, account splitting,
borrowing another worker's backing, invalid inputs, and conservative rounding.
`test_reward_eligibility.py` exercises actual PostgreSQL/SQLite backing queries
and the query-to-frozen-sender path. `test_demand_reward_periods.py` checks
expiry, policy changes, replay, immutable backing, and downgrade refusal.
These tests never broadcast a transaction. Exact non-binary persisted amounts
require PostgreSQL; SQLite NUMERIC is not equivalent money-storage evidence.

Local September 11 verification: 264 settlement tests passed on disposable
PostgreSQL 16, with no skips, after the final downgrade/rounding regressions.
The broader Grid run passed 1,755 tests with 249 environment-dependent skips
before those final refinements; full required Linux CI is still a separate
gate. Both temporary database servers shut down after testing. The staged
source secret scan and diff checks passed. This is candidate-code evidence,
not a live payout, GPU, migration deployment, or market-price proof.

## 2. Finish generation paths

All four remain disabled until individually qualified. Reuse the existing
admission gate, reservation/recovery contracts, and deployment process.

- [ ] Image batches: confirm deployed worker/recipe batch mapping, generate
  all requested distinct output slots, verify complete-batch charging and
  no payout for missing outputs, recover the original batch after reload.
- [ ] Image-to-image: use only a recipe declaring image input, prove the
  source reaches the GPU workflow, verify paid result/reload and unsupported
  model rejection. Earlier canary evidence is not current admission approval.
- [ ] Director timelines: prove a multi-segment result with requested first
  frame, duration and ordering, correct billing, timeout/reload recovery and
  no duplicate dispatch. First-frame success is not timeline success.
- [ ] 3D: establish an approved recipe and real capable worker, then prove a
  usable artifact and full paid lifecycle. Do not silently bring back a retired
  model or advertise a capability without worker coverage.

For every path: zero-credit rejection before dispatch, bounded input, actual
reserve/settle/refund, worker payout linkage, interrupted-client recovery,
negative worker result, fresh deployment/rollback evidence, and accurate UI.
Keep successful siblings available while qualifying one new capability.

## 3. Measure validator effectiveness

- [ ] Recheck fleet versions, accepted evidence, and reviewed common control;
  registrations and votes do not establish independent operators.
- [ ] Run known-good models across supported engines and quants, including
  unavailable logprobs. Report compatibility and false positives separately.
- [ ] Deliberately substitute a small model behind the claimed identity.
- [ ] Test canned-template solvers, fabricated logprobs, selective correct-model
  execution for recognizable probes, replay, and reference disagreement.
- [ ] Retain a blinded held-out workload split and report counts, detection,
  false-positive and inconclusive rates per model/backend, not one grand score.
- [ ] Keep routing/reward/slashing authority off until the evidence supports a
  reviewed policy. Strong signatures authenticate claims, not model execution.

Use the existing anti-gaming, text-fidelity, Responses qualification, and media
validation contracts. Do not add another competing validator architecture.

## 4. Independent demand and reliable coverage

- [ ] Publish accurate supported models, prices, privacy limits and setup paths.
- [ ] Follow one independent builder from login/funding through a successful
  paid request and repeat usage; maintainer canaries are not customer traction.
- [ ] Follow an independent worker from clean install to a served paid job and
  verified payout, including upgrade/restart recovery without private support.
- [ ] Measure paid consumption, distinct purchasing accounts, failure/refund
  rates, repeat demand and worker coverage. Separate sponsored and first-party
  usage and avoid treating accounts as verified independent humans.
- [ ] Expand coverage where real demand or reliability gaps justify it, not
  through an unbounded promise to distribute the full emission budget.

These outcomes require real workload and operator evidence; documents, passing
unit tests, successful cron runs, and promotional posts alone do not prove them.
