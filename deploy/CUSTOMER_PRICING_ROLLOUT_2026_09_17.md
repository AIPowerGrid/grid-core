# Customer Pricing Rollout: September 17, 2026

## Deployed State

Core now selects immutable `3384c223e268e9850177f68139e35768de04c5f7`,
Alembic `0042`. Public health and pricing were verified at 01:55:23 UTC,
with healthy Redis and all nine workers reconnected. This supersedes the
`d1aafcf4` runtime, not the frozen compensation contracts or payout controls.

PR #207 adds the exact text price for `qwen38-flash-next-125b-nvfp4`:
$0.075 input / $0.30 output per million tokens. It is an explicit launch peg,
not a competitor comparison. Unknown models remain default-denied; this does
not authorize a wildcard price or free inference. Price book is `2026-09-16-a`.
The selected main also includes the reviewed per-model validator challenge-family
rotation, which rotates issued attempts rather than successful verdicts.

## Release Proof

- Required PR CI: [35170280273](https://github.com/AIPowerGrid/grid-core/actions/runs/35170280273).
- Exact-main CI: [35171402777](https://github.com/AIPowerGrid/grid-core/actions/runs/35171402777).
- Main's Core suite: 2,141 passed, 12 skipped. Redis collector proof and the
  real-PostgreSQL Core/Console/node handoff also passed. PostgreSQL 16 is
  configured for the migration, credit, payout, validator and shadow tests.
- A fresh production backup restored into an isolated scratch database,
  migrated with the exact candidate and passed schema parity. No production
  migration was needed. Runtime dependencies came from the hash-locked wheels.
- New generation/probe ingress was temporarily gated; MCP was stopped while
  active jobs drained. Core restarted only after queues and holds were quiet.
  Readiness required the exact release and restored worker count before ingress
  reopened. This was a brief maintenance cutover, not zero downtime.
- Six Core processes matched the intended configuration. Only the observation
  HMAC changed, after closing the old run; the prior secret remains protected
  with that run's evidence. Charging remains `on`, daily free spending enabled.
- Compensation contract hashes, earning dates, budget limits, payout timer
  state and payout drop-in digest were unchanged. No backend service, client
  GPT service, worker signing identity or compensation recipient was changed.

## Customer Canary

After deployment, a synthetic request through the real authenticated Chat UI
selected Qwen and returned `QWEN-READY`. The reservation settled at 01:55:53 UTC:
9,913 micro-USD reserved, 9,829 refunded, 84 charged ($0.000084).
The owner's daily allowance had already been consumed by earlier tests, so this
was a paid canary. Read-only reconciliation found no negative balances, ledger
mismatches, stale holds or invalid pocket splits.

Seven unauthenticated empty-body generation checks retained their pre-deploy
401/422 responses. These prove unchanged rejection behavior, not successful
generation in all formats or a complete authorization audit. Earlier same-day
Chat/Music successes are separate evidence; Gallery login/generation, live
insufficient-balance behavior and live failed-generation refunds remain unproven
in this run. Do not label the entire customer launch complete.

## Observation Disposition

The owner explicitly prioritized this customer fix over the pinned observation.
`shadow_20260916_observation_v4` was closed as `cancelled` at
`2026-09-17T01:54:42.299344+00:00`, preserving its original start and records.
It is partial, never completed or review-eligible:

- 194 route observations, all with successful terminal outcomes.
- 188 insufficient-evidence decisions and six objectively healthy/same
  decisions; zero would-change or would-exclude decisions.
- Full observed successful-job and terminal capture; 142 of 145 expected
  capacity slots, maximum gap about 805 seconds.
- Zero observer errors, replay failures or mutation attempts.

This is protocol/capability monitoring, not model-identity or anti-cheating proof.
The prior report and its HMAC remain in protected operational storage. Use that
saved report; do not run old-record verification against a different release or
the replacement HMAC and mistake the mismatch for evidence corruption.

The proposed `shadow_20260917_customer_hotfix_v4` did **not** start. Its fresh
prepare gate found five verified participating operator groups and 50 finalized
independent probe groups, but the cohort monitor was critical because one
candidate had a stale heartbeat. No draft was applied and no start time exists.
The observer remains enabled but no observation run is active. Validator
assignments, signed reports and compensation collection continue independently.

Restore the candidate's actual health or resolve its status through the normal
review process, then re-evaluate a fresh exact-hash prepare/start gate. Do not
weaken the gate, erase its history, backdate the replacement, or extend the
earning period to compensate for observation downtime. Compensation's earliest
finalization remains September 22 at 15:30 UTC; observation evidence in that
report must be labeled partial. No automatic penalties or routing authority.

## Rollback

The previous immutable runtime and protected environment remain available.
An API rollback requires gated/drained ingress and preserved payout controls;
it would restore the Qwen missing-price failure. Do not resurrect the cancelled
observation or reset compensation. A new observation requires its own valid
current-release gate. There was no production rollback during this cutover.
