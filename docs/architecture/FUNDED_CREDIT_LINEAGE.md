# Externally funded credit lineage

Status: candidate, September 11, 2026. Not deployed. Depends on the prospective
demand cap in PR 178; neither change authorizes a payout or changes past plans.

## Why

Purchased-pocket value is not necessarily purchased. The production audit found
operator-issued service/bootstrap and manual grants totaling $5,200, versus
$10.02 of credited USDC receipts. These grants remain legitimate spendable
service credits, but must not support unrestricted worker emissions.

The new metadata is a subset of the existing balance, not a fourth frontend
pocket. `balance_micro` and every historical spendable delta remain unchanged.

## Invariants

- Only the verified deposit adapter explicitly creates funded credit. Calling
  the general credit helper with a deposit-looking reason still creates a grant.
- A debit consumes funded value first, up to the amount actually held. Its
  signed `funded_delta_micro` is recorded atomically with the ordinary debit.
- A refund restores only unused funded value from that account's original
  debit. Duplicate refs cannot restore it twice. The grant portion is not
  promoted on refunds, failures, or retries.
- Account merges move both subsets through paired append-only entries. They
  still refuse in-flight reservations and preserve the old job/deposit records.
- A new reward cap counts net funded debit/refund/extra movements for the exact
  reservation account and job, only when net spendable movements agree with the
  terminal charge. Missing or contradictory movements provide zero backing.
  x402 retains its separately verified settled-payment requirement.
- SQL constrains funded balance to the nonnegative spendable subset. The
  existing billing monitor independently compares both caches with their
  ledger sums, per account in one consistent snapshot.

This is accounting provenance, not independent-customer proof, a token-price
oracle, or model verification. AIPG/ETH funding retains its deposit-time
valuation risk. The prospective reward epoch still needs reviewed valuation.

## Existing balances

Migration 0042 adds zero-default metadata. It does not invent provenance for
old balances. `funding_lineage.opening_plan` replays the old ledger in ID order:
verified Base receipt/ref/account/amount matches create funding; debits consume
it; known refunds return the unused portion; paired account merges move it.
Other positive entries remain grants. Missing receipts, orphan refunds/merges,
negative reconstructed balances, cache drift, or existing funded history stop
the process for review. Nothing is silently repaired.

The private command defaults to a repeatable-read, read-only preview on
PostgreSQL. It returns aggregate amounts and a hash of the complete bounded
snapshot, not account identities. Snapshots exceeding 100,000 rows in any of
the three tables require a separately reviewed scale-up, never truncation.
Apply requires that exact hash under write-excluding table locks. It appends
zero-spendable `funding:opening` entries and updates funded caches in one
transaction. It cannot increase user credit, rewrite history, or send tokens.
Replaying a committed hash returns `already`, including after later spending.

Tested command interface (disposable databases only):

```sh
python -m grid_api.services.funding_lineage
python -m grid_api.services.funding_lineage --apply REVIEWED_SHA256
```

## Deployment gate

1. Complete candidate review, PostgreSQL migration/schema parity, concurrency
   tests, and a read-only historical production audit. Build an immutable
   release containing both this change and the reviewed demand cap.
2. Schedule a short controlled drain: close new generation admission and
   funding claims, let existing jobs settle/release, then pause all credit
   writers, including operator grant jobs. Do not discard held reservations.
3. Back up and apply 0042. With writers still paused, run the candidate's native
   opening preview, review the exact amounts/hash, then apply that hash.
4. Deploy all lineage-aware writers before resuming funding and generation.
   Verify funded and ordinary ledger/cache health, a real funded reserve/refund,
   and a grant-only job with zero new-cap backing. Historical v1 plans remain
   unchanged. Keep demand policies empty until the separate activation review.
5. Select/announce a prospective reward epoch with reviewed valuation and
   expiry, compare allocations, then run the bounded payout/replay canary.

After funded history exists, an old credit writer is NOT a safe rollback:
it can omit provenance even if spendable balances still reconcile. Retain the
lineage implementation and schema; disable new admission/payout activation
while investigating. Downgrade refuses nonzero funded history, including
fully spent deposits. Do not zero metadata to bypass the guard.

## Verification

`test_funded_credit_lineage.py` covers grant-label spoofing, full/partial
refunds, durable terminal settlement, duplicate and insufficient debits,
merges, the subset constraint, migration preservation, spent-history downgrade
refusal, monitoring drift, and 25-way real PostgreSQL debit races.
`test_funding_opening.py` covers receipt/refund/merge replay, refusal paths,
preview/apply drift, immutable history, retry after spending, in-flight holds,
and concurrent opening applies. Existing deposit tests prove atomic/idempotent
receipt-driven funded credit. Reward tests prove grant exclusion and exact
net-consumption accounting through the real aggregation query.

These tests are not production deployment or economic activation evidence.

Final local candidate verification: a fresh PostgreSQL 16 database upgraded
through 0042 with `alembic check` reporting no new operations. The full Core
suite passed 1,807 tests with 251 environment-dependent skips and 39 existing
deprecation warnings. New funding and opening races ran on PostgreSQL; their
SQLite variants deliberately skip concurrency rather than simulate it on one
connection. Required Linux CI remains a separate release gate.

September 11 read-only production replay: six balance rows totaling
5,210,133,096 micro-USD reconciled with their immutable ledger; credited
deposits totaled 10,020,000 micro-USD and the reconstructed unspent funded
subset was 9,610,843 micro-USD. No held reservation blocked that snapshot.
No balance, metadata, receipt, setting or service was changed. This aggregate
audit is not the native post-migration apply hash; take a fresh preview during
the controlled drain because new spending invalidates earlier snapshots.
