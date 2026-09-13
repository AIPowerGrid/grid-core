# Externally funded credit lineage

Status: deployed September 13, 2026 UTC on immutable `93a21eec` / Alembic
`0042`. PR 179 includes the prospective demand cap from PR 178; do not deploy
PR 178 alone. The maintainer approved a separate 24-hour reward pilot, recorded
below. Deployment does not reprice or authorize historical payouts.

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
- The same SQL snapshot computes each job's funded DEN fraction and its cap
  backing. Unfunded grant traffic cannot inflate the denominator and dilute
  other workers, including when one account serves both funded and grant jobs.
  SmolLM-family weights use the same funded fraction; old v1 weights do not
  change.
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

Tested command interface (also used during the controlled production opening):

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

Final review additionally reproduced grant-denominator dilution in both
grant-only and mixed-funded accounts: large grant traffic removed honest
workers from the payable result despite providing no cap backing. New v2
allocation now uses only funded DEN and reads weights/backing together. The
regressions fail on the preceding candidate. The following full-suite result
includes that refinement and its non-finite-weight guard.

Final local candidate verification: a fresh PostgreSQL 16 database upgraded
through 0042 with `alembic check` reporting no new operations. The full Core
suite passed 1,813 tests with 251 environment-dependent skips and 39 existing
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

## Production preparation, not cutover

This section records September 11 preparation. The following September 13
cutover supersedes its deployment status, not its rollback warnings.

On September 11, candidate `7cb7465e` was built as a separate immutable release
using the reviewed hash-locked dependencies; `pip check` passed. A new
root-only production backup at `23:33:49Z` restored into a generated scratch
database, upgraded through 0042 and passed schema parity. The restore tool
removed its scratch database. Production remained on `793fe904` / 0041 with
the previous charging and payout-timer state.

That built candidate predates the grant-denominator refinement. Do not select
it merely because its migration proof and CI passed: build and requalify the
final reviewed commit before any cutover. No live opening entries, funded
cache updates, or reward-policy changes have been applied.

## September 13 production cutover and pilot

The final reviewed merge `93a21eec18e583e9c2bda8e914828434accb9d13` passed
required Linux/PostgreSQL CI before deployment. A fresh production backup
restored and migrated in scratch. Admission and funding claims were temporarily
closed, MCP and the payout timer paused, queues drained, and all credit writers
stopped before the live migration and opening. Both queue streams had zero
pending/undelivered work and there were no held reservations.

The native opening preview/apply matched six balances, 5,210,133,096 micro-USD
spendable, 10,020,000 micro-USD verified deposits, and 9,610,843 micro-USD
unspent funded value. Repeating the reviewed hash returned `already`.
Fingerprints proved the original spendable balances, historical credit ledger,
all payout rows and all 59 frozen plans unchanged. No payout was broadcast.

Two real loopback-API canaries ran through production workers behind the
maintenance gate, using temporary expiring keys revoked afterward:

| Demand source | Reserved micro-USD | Actual | Refund | New reward backing |
| --- | ---: | ---: | ---: | ---: |
| Verified funded owner credit | 40 | 6 | 34 | 6 |
| Existing grant-only operator service | 40 | 6 | 34 | 0 |

The grant-only job supplied no funded DEN weight either. Both spendable and
funded caches reconciled to their ledgers, with no negative balances, stale
holds or invalid reservation splits. These two tests spent 12 micro-USD in
total, only six from externally funded credit; they were not customer traction.

Public traffic reopened at `2026-09-13T01:20:40Z`. All six API processes use
the new release and `GRID_CHARGING_MODE=on`. The existing generation admission,
promotion/service policy and immutable paid-only cutoff were preserved. MCP,
the prior database statistics schedules and payout timer resumed.

Approved prospective reward policy:

- Window: `2026-09-13T02:00:00Z` through `2026-09-14T02:00:00Z`.
- Accounting valuation: 2,000 micro-USD per AIPG ($0.002), not a market oracle.
- Worker share: 8,500 basis points (85%) of each worker's backed consumption.
- Existing 208.33 AIPG hourly ceiling, minimum payout and SmolLM cap retained.
- No backing means no new allocation; unused budget is not redistributed.

A production systemd service drop-in, `40-demand-pilot-start.conf`, prevents
the automated sender from running before `2026-09-13T03:00:00Z`, the first
complete pilot hour. Its skip was executed and verified. This intentionally
does not send the pre-pilot canary hour under legacy fixed-budget weights.
It does not edit prior plans or authorize backpay. Preserve this additional
deployment gate when reconciling units; the versioned base unit is unchanged.

Native preview of the prospective first hour returned zero allocations and the
entire ceiling unallocated. The expiry guard rejected a new hour beyond the
approved window. Keep the policy history and lineage-aware sender: never clear
the policy to bypass expiry or restore an old credit writer.

Remaining evidence: observe the first complete pilot period, then a nonzero
eligible frozen allocation, exact Transfer verification and an idempotent
replay. The six-micro funded canary is below the retained minimum payout and
cannot prove that transfer path. Do not call the 24-hour observation or a
nonzero capped payout complete from these deployment checks.
