# Validator Period Rollout: September 15

## Live Release

Core `64d3895184536ebbb780f25bbf42883c9884423d` / Alembic `0042` was selected
at **2026-09-15 21:38:17 UTC**. This deploys PR #194 only: verified v8 text
evidence selection, runtime-derived shadow policy defaults, and the read-only,
no-DDL shadow operator CLI. It does not enable observation or change customer
routing, charging, rewards, operator qualification or compensation contracts.

PR #195 is a **draft and must not be deployed**. Its global single-tool-choice
change passed LM Studio controls but failed additional DeepSeek controls; that
runtime proposal needs a narrower or replacement design.

## Verification

- Required PR CI passed. Exact-main [CI run 35025010161](https://github.com/AIPowerGrid/grid-core/actions/runs/35025010161)
  passed: 2,103 tests, 11 skipped tests, and one
  additional real PostgreSQL/Core/Console/node handoff test. The workflow sets
  the disposable PostgreSQL shadow-test URL, so its concurrency tests are not
  part of the optional skips.
- Built a clean detached release with the hash-locked binary-wheel dependencies;
  `pip check` passed. Schema and dependency lock were unchanged.
- A fresh production backup at 21:35:53 UTC restored into a generated scratch
  database; candidate migration, Alembic `0042` and schema parity passed. The
  restore tool removed the scratch database. No production schema change.
- A temporary ingress gate paused new generation and validator probe dispatch.
  MCP stopped, two consecutive quiet checks passed, then a final check after
  Core stopped found zero held reservations, zero pending jobs, and no actual
  undelivered text/media entries. Text's historical Redis lag counter remained
  nine; direct stream inspection found no entries beyond the delivered cursor.
  No stream cursor or queue entry was edited to manufacture a drain.
- Atomic release selection, Core/MCP startup, health and Nginx validation passed;
  the temporary gate was removed. All nine workers reconnected.
- The production environment and payout-drop-in hashes, plus payout timer
  enabled/active state, were unchanged. No payout was manually retried.
- Public health and network status report the exact deployed commit. An
  authenticated read through an existing owned worker credential returned 200;
  unauthenticated validator assignments returned 401 and the retired heartbeat
  path returned 410. No customer generation was submitted by these checks.

## Observer Gates And Limits

Post-deploy read-only evaluation at 21:39 UTC found five reviewed participating
operators and 51 independently supported finalized groups under a proposed
preview.20 policy. The earlier count was 52; this is a moving freshness window,
not the frozen 33-failure audit denominator.

The observer remains disabled, with zero runs and no HMAC configured. The actual
baseline remains preview.13 with the reviewed .15-.20 upgrade overlap. A stale
candidate still causes the existing critical cohort gate to fail. Preserve its
review and qualification history; restore the node or explicitly resolve the
incident, never erase it to pass the gate. Diagnostic proof booleans were left
false because these calls were not an activation proposal.

Before starting: complete cohort recovery/review, deliberately freeze .20 and
remove upgrade overlap, retain the protected HMAC, record the exact CI and
restore proof, enable and verify the inert collector, then use digest-bound
prepare/start. No automatic routing or economic authority follows.

The live snapshot had **13 advertised models, each with one worker**. A
same-model replica experiment therefore has no alternative replica to choose
for those models. Report capacity shortages and observed failure correlations;
do not claim routing improvement from a no-choice sample. Real same-model
redundancy is needed to evaluate alternative-worker recommendations.

## Additional Backend Controls

The frozen DeepSeek tool-call and first-stage tool-chain requests were repeated
with only the single-tool choice changed to `required`, in streaming and
non-streaming modes. Of four controls, only the non-streaming single call
passed. Both streamed controls returned reasoning but no call; the non-streaming
chain also returned no call. No second stage was reached. This does not qualify
the global generator change or establish a repair of either original defect.

Private capture SHA-256:
`c38fa65f7765c0257b93760a89629a03f3891cad01490a899b0461b537e7219a`.

LM Studio's separate successful controls remain valid, but are not evidence
about another engine. Preserve all original and corrected-request captures,
without rewriting historical verdicts or classifying these failures as fraud.

## Remaining Deliverables

The frozen failure ledger still needs completed root-cause dispositions and
backend repairs. Controlled functional faults and one small-model impersonation
trial are complete, not broad model-fidelity qualification. The seven-day
observer has not started. The September 22 report must say what was actually
observed; a later experiment start means a later experiment finish, not a
backdated or shortened run. Payment allocations remain subject to the unchanged
campaign end and receipt grace, separate recipient consent and transfer review.
