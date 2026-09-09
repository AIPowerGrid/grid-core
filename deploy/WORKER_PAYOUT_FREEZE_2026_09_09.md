# Frozen worker payout periods - deployed, senders paused

## Posture

Production selected Core `3ab6d933` / Alembic `0041` at
`2026-09-09T22:44:38Z` after required CI and a restored-production migration
proof. Charging remains allowlisted and worker payout service/timer paused.
The new plan table is empty; historical payout fingerprints are unchanged.
See the launch evidence for backup, process and balance reconciliation.
No historical backpay, treasury refill or sender restart is authorized here.

## Contract

- New sends require a closed, whole UTC hour after the existing immutable
  paid-only reward cutoff. The crossing hour is excluded, not partially paid.
- The ID must be `hour-YYYY-MM-DDTHH`; alternate IDs and partial/overlapping
  windows reject. `grid_payout_periods.utc_hour` is unique, including hours
  with no eligible allocations.
- `PAYOUT_HOURLY_BUDGET` is both the wrapper's request and the sender's ceiling;
  its existing default remains 208.33 AIPG. A lower requested budget is allowed,
  but a retry cannot change it. No emissions increase is introduced.
- The plan commits all account allocations, original destinations, DEN,
  amount, cutoff, token, Base chain, minimum and SmolLM cap policy. The complete
  plan and all `grid_payouts` rows commit atomically before any broadcast.
  Late jobs or changed configuration cannot reprice it. A commitment/row
  mismatch fails closed before invoking a sender.
- Amount, DEN, non-null recipient and assigned nonce cannot be rewritten.
  Walletless new accrual may bind its first current account wallet once.
  An unbound `pending` row is recoverable after a crash before signing; it
  must not be interpreted as proof of an on-chain broadcast.
- New send, accrued and retry functions themselves share the treasury's
  PostgreSQL transaction advisory lock, including imported calls. The CLI
  does not need a second lock. Raw token units use Decimal arithmetic.
- Automatic accrued/retry queries require a verified prospective plan.
  Historical rows receive no plan and remain unchanged, including disputed
  payments and owner accrual. A separate reviewed historical settlement
  procedure is required; never fabricate plans to bypass this exclusion.
- The hourly shell wrapper captures the clock once and derives all fields
  from that UTC timestamp. No service-key, credit or reward-cutoff setting
  changes accompany the wrapper change.

## Verification

- Expanded sender/period/reward/multiasset/revenue/screening suite:
  **193 passed, no skips**, using isolated local PostgreSQL 16.15 for the
  configured payout and reward fixtures. Chain responses are synthetic.
- Ten simultaneous plan creations produce one committed snapshot and one
  aggregation; competing imported send/accrued/retry calls cannot enter while
  the first sender holds the treasury lock. A later call succeeds after release.
- Reproduced partially-paid 100-token period: late allocation changes do not
  create the old 125-token outcome; changed wallets/budgets cannot redirect
  or reprice the remaining payment.
- Snapshot insertion failure rolls back the plan and all allocations together.
  Empty periods remain empty on replay. Frozen previews do not reaggregate.
- Fresh PostgreSQL migration to `0041`, empty-table downgrade to `0040`,
  re-upgrade and `alembic check` passed. Actual migration code refuses to
  discard a nonempty plan in the DB-backed test.
- The wrapper test runs only a copied script with an argument recorder in
  place of Python and a controlled clock; no test invokes the production
  sender. ShellCheck passed.

Local Python 3.13 proof does not replace required Python 3.12/locked-dependency
CI, restored-production-schema proof, or a reviewed live readback.

## Deployment And Rollback

1. Keep all worker senders paused. Preserve exact reward cutoff, balances,
   ledger history, pending hashes/nonces and historical obligations.
2. Take the protected production backup and prove restoration/migration using
   the exact reviewed candidate. `0041` must add an empty table and must not
   adopt or rewrite existing payouts.
3. Apply `0041` before selecting the candidate sender. Verify schema parity,
   exact release/configuration, unchanged historical row counts/commitments,
   and the configured hourly cap. Do not run an old retry command.
4. Reconcile a closed prospective hour against purchased backing and its
   fixed budget. Review recipients and treasury balance before a supervised
   send; verify on-chain results and replay the same hour to prove no resend.
5. Resume the timer only after those live checks and the broader demand launch
   gates pass. Keep excluded historical backpay separate.

API rollback may retain the additive table, but keep payouts paused. Older
worker senders can bypass frozen plans and select excluded historical rows.
Never downgrade nonempty plans or edit them to force a replay to succeed.
