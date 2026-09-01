# Validator Shadow Authority

## Status

Accepted rollout contract with a disabled-by-default Core implementation;
collection is not live. Migration `0032`, the pure policy, Core-derived evidence
snapshot, append-only replay store, live start-gate evaluator, aggregate report
CLI, import isolation guard, and SQLite/PostgreSQL concurrency tests exist.
Production validator evidence remains observability-only and the real router
does not read it. Shadow mode may start only after three recently participating,
independently reviewed operator groups complete the validator cohort gate. It
runs for seven days without changing routing, rewards, worker status, den,
payouts, bonds, strikes, or slashing.

Shadow mode is an evaluation phase, not partial authority. Completing it creates
a review artifact; it does not enable routing or validator economics.

## Purpose

The shadow observer answers one bounded question:

> If the reviewed validator policy had advisory routing authority, would it have
> changed the route selected by the existing production router?

For every eligible production routing opportunity, Core retains the actual
choice and computes a hypothetical choice from the same frozen candidate set.
The comparison measures coverage, disagreement, stability, and likely impact
before validator evidence can influence users.

## Invariants

1. **No hot-path dependency.** The real router completes from its existing
   curated and Grid-measured inputs. Shadow calculation is asynchronous and a
   failure cannot fail, delay, retry, or change a user request.
2. **No shared write path.** Shadow records are append-only observations. The
   router, worker health, settlement, credits, payouts, rewards, bonds, strikes,
   and slashing code must not import or query them.
3. **Independent evidence only.** First-party nodes, unreviewed registrations,
   unsupported releases, stale reviews, and duplicate control groups never fill
   a shadow quorum seat.
4. **Fail closed to no opinion.** Missing quorum, stale evidence, reference
   disagreement, an unknown policy, or insufficient samples produces
   `insufficient_evidence`, never a negative worker decision.
5. **Current text scope stays narrow.** Public-template text probes may support
   protocol-conformance or declared-capability observations. They cannot create
   a general quality rank or prove a model family, parameter count, or quant.
6. **One frozen policy.** The routing policy, evidence window, thresholds, and
   configuration hash are fixed before a seven-day run. Changing any of them
   starts a new run.
7. **No automatic promotion.** A successful report permits a separate routing
   design review only. Rewards, staking, strikes, and slashing remain separate
   projects with their own gates.

## Start Gate

The maintainer may begin a shadow run only when all of these are true:

- at least three distinct operator groups are `verified`, their reviews are
  current, and they are recently participating with the required cohort release;
- real shared probe groups have finalized with at least three eligible
  independent votes;
- the cohort monitor reports no unresolved critical assignment, evidence,
  version-drift, or duplicate-control incident;
- Core schema and the shadow implementation have passed PostgreSQL migration,
  concurrency, replay, and no-side-effect tests;
- the observation policy and configuration hash are recorded; and
- production routing and validator economic effect still report `none`.

The draft gate is derived from current Core records rather than supplied
operator counts. Starting a draft re-derives the gate, freezes that fresh
snapshot, and fails if capacity or cohort health changed between review and
start. PostgreSQL migration/concurrency, replay, and no-side-effect proofs remain
explicit reviewed inputs referenced by the run artifact.

Five independent operators remain the broader pilot target. Three is enough to
start this bounded readiness experiment, not enough to claim mature fault
tolerance.

## Eligible Evidence

A shadow opinion may use only a finalized probe group that:

- binds the same worker, model, modality, capability, and scoring-policy version;
- contains at least three votes from distinct, currently reviewed control groups;
- contains valid assignment, nonce, evidence-hash, and signature bindings;
- was created after each contributing operator's qualification began;
- is within the frozen evidence-freshness window; and
- has an evidence dimension allowed by the shadow policy.

`disputed`, `inconclusive`, reference-unavailable, and Core/verdict-disagreement
groups remain visible but cannot become a worker-negative opinion. Preview rows
and ordinary registration quorum are never promoted by inference.

The implemented collector boundary does not accept a caller-provided
`bindings_valid` assertion. Core joins finalized probe groups to their exact
assignments, authoritative attestations, and registered validators, then
rechecks signature status, assignment/validator/group identity, Grid nonce,
evidence hash, Core probe verdict, worker/model/modality/capability/policy
bindings, qualification timing, frozen software version, heartbeat freshness,
review status, review expiry, and distinct opaque operator groups. Only the
resulting bounded commitment and count enter the replay snapshot.

## Decision Record

Each append-only observation must be replayable from bounded identifiers and
commitments without retaining customer prompts or outputs. At minimum it records:

- run id, UTC time, policy version, and configuration hash;
- task class and requested capability;
- a privacy-safe commitment to the frozen candidate set;
- actual model and worker selected by the production router;
- hypothetical model and worker, or `insufficient_evidence`;
- decision class (`same`, `would_change`, `would_exclude`, or
  `insufficient_evidence`);
- bounded reason code and evidence-window bounds;
- finalized group commitments and distinct eligible control-group count;
- actual terminal outcome when it later becomes available; and
- confirmation that no routing or economic mutation was attempted.

Public summaries aggregate these fields. They never expose prompts, outputs,
worker or validator wallets, account ids, validator ids, control-group ids,
signatures, nonces, private review references, IPs, or host data.

## Seven-Day Observation

The run lasts at least 168 wall-clock hours. Core derives independent capacity
from current reviewed registrations and finalized evidence at bounded intervals;
callers cannot supply the counts. Missing expected five-minute slots count
against coverage, and gaps are recorded rather than filled with first-party
votes. The report is eligible for review only when:

- at least 80 percent of observation samples have three recently participating
  independent groups;
- no continuous independent-quorum gap exceeds one hour;
- every hypothetical change has a replayable evidence and policy commitment;
- observer errors, stale evidence, and insufficient-evidence rates are reported;
- actual versus hypothetical route counts and terminal outcomes are reported by
  model, capability, and bounded reason code;
- would-change, reversal, disagreement, and coverage rates are stable enough to
  explain rather than hidden in a global average; and
- an automated invariant test and production audit confirm zero real routing or
  economic reads from the shadow records.

The report also requires terminal outcomes for at least 80 percent of recorded
observations. A run marked `completed` means only that its frozen 168-hour window
elapsed and was closed. Only the separate `review_eligible` gate means the
coverage, outcome, replay, gap, and zero-mutation checks all passed; neither state
promotes the observer automatically.

If the capacity or integrity gate fails, the run is evidence about readiness but
does not satisfy the seven-day milestone. Restore the cohort, freeze a new run,
and repeat; do not lower the gate after seeing the result.

## Interpretation

The final report must separate:

- protocol/capability compliance from subjective quality;
- worker-level evidence from model-level inference;
- lack of evidence from negative evidence;
- independent votes from first-party telemetry;
- actual production outcomes from the hypothetical policy; and
- observed correlation from a claim that validator advice caused an outcome.

A policy that mostly reports `insufficient_evidence` may still prove the
observer safe, but it does not prove routing usefulness. A policy that would
change many routes without matching later Grid-measured outcomes is a reason to
revise and rerun, not to activate it.

## Implementation Order

1. **Implemented dark:** pure, versioned advisory-policy function over frozen
   candidate and Core-derived finalized-evidence inputs.
2. **Implemented dark:** append-only observation store, outcome/capacity/error
   records, replay, start-gate evaluation, aggregate reporting, migration, and
   no-side-effect/import/concurrency tests.
3. **Next, after review:** run the observer from a durable background/outbox
   boundary after the real route is selected; never call it from the
   route-critical transaction.
4. Add the route/outcome outbox integration and prove every user-visible and
   economic output remains byte-for-byte unchanged under collection faults.
5. Dark-deploy with collection disabled and verify migration/rollback on the
   production-shaped release.
6. After the three-operator gate, freeze one policy and start the seven-day run.
7. Review the report before discussing any routing-weight experiment. Validator
   rewards remain out of scope.

This implementation belongs to Core. The public validator binary continues to
submit the same signed evidence and does not need a new release for shadow
collection. In particular, do not publish preview.14 merely to begin this work;
preview.13 remains the cohort baseline while an operator is qualifying.

`VALIDATOR_SHADOW_RETENTION_DAYS` reserves the operational policy value, but no
pruner is wired yet. Do not claim retention enforcement until the future
collector owns and tests that cleanup path; existing shadow evidence remains
append-only in this dark foundation.
