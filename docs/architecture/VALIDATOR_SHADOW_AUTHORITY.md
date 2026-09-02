# Validator Shadow Authority

## Status

Accepted rollout contract with a disabled-by-default Core implementation;
collection is not live. Migration `0032`, the pure policy, Core-derived evidence
snapshot, append-only replay store, live start-gate evaluator, aggregate report
CLI, import isolation guard, asynchronous Redis route-event outbox and isolated
collector, and SQLite/PostgreSQL concurrency tests exist.
Migration `0033` additionally enforces one running experiment at the database
boundary, and `0034` adds a stable private per-job commitment for exact ledger
coverage; lifecycle transitions reject backdated or future-dated operator time.
Production validator evidence remains observability-only and the real router
does not read it. Shadow mode may start only after three recently participating,
independently reviewed operator groups complete the validator cohort gate. It
runs for seven days without changing routing, rewards, worker status, den,
payouts, bonds, strikes, or slashing.
The operational procedure is [VALIDATOR_SHADOW_RUNBOOK.md](../VALIDATOR_SHADOW_RUNBOOK.md).

Core `e1e4ad4c9eeb277f385a2359f3bc418917a7f0e1` was dark-deployed on
2026-09-01 with Alembic `0034`. A production-backup restore traversed
`0031 -> 0032 -> 0033 -> 0034` with no schema drift before the live migration.
The observer flag remained false, every shadow table remained empty, and the
route-event stream was absent after deployment. A later read-only invocation of
the immutable release's gate tool rejected startup on exactly the three cohort
requirements: verified independent operators, participating independent
operators, and a finalized independent probe group. That is deployment and
fail-closed evidence only; collection has not started.

Shadow mode is an evaluation phase, not partial authority. Completing it creates
a review artifact; it does not enable routing or validator economics.

## Purpose

The shadow observer answers one bounded question:

> Given the concrete model and worker that pull-based production dispatch
> actually used, would the reviewed validator policy have preferred another
> connected, protocol-compatible replica sampled near that dispatch?

For every captured production route, Core retains the actual choice and computes
a hypothetical preference from a frozen post-dispatch replica sample. The
comparison measures coverage, disagreement, and signal stability before
validator evidence can influence users.

Production uses worker-pull Redis streams; it does not materialize or rank one
central candidate set before dispatch. The background observer therefore cannot
replay an exact scheduler decision. Its sample may include a compatible worker
that was busy or had a prefetched job, and a connection may appear or disappear
between dispatch and the asynchronous snapshot. The two-second registry cache
also means the sample can lead or lag dispatch slightly. The frozen policy names
this basis `post_dispatch_connected_compatible_replicas.v1`, and the collector
rejects events or runs that claim another basis. A report from this phase is
evidence about same-model replica preference, not proof that production would
have made the hypothetical route.

The first collector deliberately freezes only connected, protocol-compatible
replicas of the concrete model that production selected. It can measure whether
validator evidence would have avoided one worker replica; it does not yet
second-guess the earlier `auto` model-family choice. Cross-model advice requires
a separately frozen pre-resolution candidate contract and is not implied by this
run.

## Invariants

1. **No hot-path dependency.** The real router completes from its existing
   curated and Grid-measured inputs. Shadow calculation is asynchronous and a
   failure cannot fail, delay, retry, or change a user request.
2. **No shared write path.** Shadow records are append-only observations. The
   router, worker health, settlement, credits, payouts, rewards, bonds, strikes,
   and slashing code must not import or query them.
   Worker transport performs only a non-awaited handoff to a neutral route-event
   emitter after actual dispatch. The handoff first reduces the live job to a
   bounded envelope, so queued observer tasks retain no prompt, output, account,
   wallet, or raw job identifier. The emitter snapshots compatible connected
   replicas in a tracked background task and commits only bounded, HMAC-linked
   metadata to a private Redis Stream. Once Redis accepts an event, consumer-group
   delivery retains it until the collector acknowledges and deletes it. The stream
   has a 10,000-event emergency bound, so an extended collector outage can trim
   the oldest pending evidence. A process crash before acceptance can also lose an
   event. Every route attempt carries a delivery-specific HMAC plus a stable,
   domain-separated per-job HMAC. The final report matches successful job
   commitments against the independent completion ledger, deduplicates retries,
   and fails review below the frozen threshold. Aggregate event counts cannot
   satisfy this gate.
3. **Independent evidence only.** First-party nodes, unreviewed registrations,
   unsupported releases, stale reviews, and duplicate control groups never fill
   a shadow quorum seat.
4. **Fail closed to no opinion.** Missing quorum, stale evidence, reference
   disagreement, an unknown policy, or insufficient samples produces
   `insufficient_evidence`, never a negative worker decision.
5. **Current text scope stays narrow.** Public-template text probes may support
   protocol-conformance or declared-capability observations. They cannot create
   a general quality rank or prove a model family, parameter count, or quant.
6. **One frozen policy and implementation.** The routing policy, evidence
   window, thresholds, configuration hash, and full Core implementation commit
   are fixed before a seven-day run. Core proves the executing release identity
   at every lifecycle, append, and report boundary. An unprovable or different
   runtime fails closed rather than mixing observer implementations. Changing
   any frozen input starts a new run.
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
- a privacy-safe commitment to the frozen post-dispatch replica sample;
- a delivery-specific route commitment and stable per-job commitment;
- the fixed candidate basis recorded by the run policy;
- actual model and worker used by pull-based production dispatch;
- hypothetical model and worker, or `insufficient_evidence`;
- decision class (`same`, `would_change`, `would_exclude`, or
  `insufficient_evidence`);
- bounded reason code and evidence-window bounds;
- finalized group commitments and distinct eligible control-group count;
- actual terminal outcome when it later becomes available; and
- confirmation that no routing or economic mutation was attempted.

The route commitment binds the private job id, Redis stream, and delivery id.
The job commitment binds only the private job id under a separate HMAC domain.
Retries therefore produce distinct route attempts without inflating the count
of ledger jobs covered by the observer. Raw job ids, prompts, outputs, accounts,
wallets, worker names, and validator identities never enter the outbox. The
HMAC secret must remain stable for the full run and archived final-report proof,
and is injected through the production secret path. Without that exact secret,
Core fails closed instead of claiming route coverage.

Public summaries aggregate these fields. They never expose prompts, outputs,
worker or validator wallets, account ids, validator ids, control-group ids,
signatures, nonces, private review references, IPs, or host data.
The exact-coverage report is `aipg.validator.shadow-report.v2`; its route count
is telemetry, while its coverage gate uses unique matched job commitments.
Every report must expose `candidate_basis` and the bounded counterfactual scope;
neither may be relabeled as exact scheduler replay during review.

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
- at least 80 percent of distinct independently recorded successful ledger jobs
  have an exact matching successful job commitment; duplicate retry attempts
  count once;
- at least one real successful production ledger job exists in the run window,
  no captured successful commitment is unmatched, and no observer error was
  recorded;
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
3. **Implemented dark:** after a compatible worker receives a real job, a
   non-awaited producer writes a bounded privacy-safe route/outcome event to a
   dedicated Redis Stream. A leased background consumer alone imports the shadow
   store, derives authoritative evidence, persists observations/outcomes, samples
   independent capacity, retries transient faults, and drops malformed poison
   events without touching production authority.
4. **Implemented dark:** static isolation plus producer/consumer fault and burst
   tests prove collection calls are never awaited by worker transport. A
   two-second, single-flight minimal worker-registry snapshot prevents route
   bursts from multiplying Redis registry scans. Candidate events are capped at
   256 entries and 128,000 UTF-8 bytes on both sides of the outbox contract.
   The policy and every route event identify this as a post-dispatch
   connected-compatible replica sample. The collector fails closed on a
   mismatched basis, and the final report repeats the limitation.
   Full worker-transport regression and production-shaped fault verification
   remain release gates.
5. **Implemented dark:** Core `e1e4ad4c` and schema `0034` are production-live
   with collection disabled; backup/restore, migration, drift, empty-state, and
   fail-closed live-gate checks passed.
6. **Next:** after the three-operator gate, freeze one policy and start the
   seven-day run.
7. Review the report before discussing any routing-weight experiment. Validator
   rewards remain out of scope.

This implementation belongs to Core. The public validator binary continues to
submit the same signed evidence and does not need a new release for shadow
collection. In particular, do not publish preview.14 merely to begin this work;
preview.13 remains the cohort baseline while an operator is qualifying.

The Redis outbox is capped at 10,000 events and acknowledged events are deleted
after leaving its consumer pending list. `VALIDATOR_SHADOW_RETENTION_DAYS`
reserves the SQL evidence policy value, but no SQL pruner is wired yet. Do not
claim SQL retention enforcement until an explicit archive/delete policy preserves
the append-only review contract and is tested; existing shadow evidence remains
append-only.
