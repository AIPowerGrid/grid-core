# Validator Observation Activation: September 16, 2026

## September 17 Disposition

The owner approved prioritizing the Qwen customer pricing fix. This run was
closed as cancelled/partial at `2026-09-17T01:54:42.299344+00:00`, not completed.
Core now runs `3384c223`; original evidence and the protected run HMAC remain
preserved. Its replacement has not started because a stale candidate blocks
the fresh cohort gate. Compensation dates and budgets are unchanged. See the
[September17 rollout record](CUSTOMER_PRICING_ROLLOUT_2026_09_17.md).
The remaining sections document the historical activation, not current status.

## Actual State

- Run: `shadow_20260916_observation_v4`.
- Start: `2026-09-16T13:49:46.014778+00:00`.
- Scheduled end: `2026-09-23T13:49:46.014778+00:00`.
- Unchanged implementation: `d1aafcf4ab9727ddfc273e76c2e55e794816eba0`;
  Alembic `0042`.
- Policy: `aipg.validator.shadow.protocol-capability.v4`.
- Config hash: `1c167fd0ccabdb4232038e40168975395b83e2ea7ec4cb738c4f5ba9c4441dab`.
- Start gate: `a6c22cda963d6756b9d406d3a9a4cb93b521e6aa7cb37d5aa0b46ddf192081d5`.

This is observation, not live routing selection. No automatic promotion,
penalties, slashing, new compensation members or transfers were enabled.
September22 compensation/report deadlines have not moved; that report must
label the observer evidence partial, rather than claim seven elapsed days.

## Gates And Changes

Fresh preflight passed with five reviewed participating operator groups and
41 eligible finalized groups. A previously stale candidate recovered its
heartbeat but remained below the required availability coverage; it was not
promoted. Another technically ready candidate remained unverified pending
control review. No qualification gate, history or evidence was weakened.

The exact-release proof is bound to
[Core CI run 35029833688](https://github.com/AIPowerGrid/grid-core/actions/runs/35029833688).
A fresh production backup and isolated restore/migration/drift proof preceded
activation. Protected evidence retains the restore output and reviewed proofs.

Only five environment fields changed: baseline to `v0.1.0-preview.20`, singular
upgrade to empty, upgrade list to `[]`, observer enabled, and a new on-host
route HMAC secret. Sampling remains 300 seconds. Six running Core processes
matched the intended configuration. Never publish or rotate the HMAC during
this run; retain it with protected evidence for coverage reconciliation.

Generation/probe ingress was temporarily gated, Core queues and held
reservations checked quiet, and Core restarted. MCP was briefly stopped and
restored. Nine workers and eleven advertised models returned. No model backend
or shared worker bridge was restarted. The client GPT service is explicitly
off-limits; no installation, restart or canary was performed on it.

Charging controls, payout drop-in digest, active/enabled payout timer and
compensation contracts were preserved. No new paid generation was submitted
for activation. Empty-body/unauthenticated route checks retained their prior
401/422 results; these are not successful modality-generation tests.

## Guarded Aborts

The first attempt checked the maintenance response before the Nginx reload
became visible and aborted before changing Core configuration. The next
attempt used a bounded readiness check and prepared one inert draft, but its
start apply correctly rejected a changed gate snapshot. It rolled back.
A transient health 502 was observed during that rollback startup; this was
not an interruption-free operation.

The successful attempt reused the same draft, took a fresh preview and
passed the guarded start on its first apply. Rollback readiness handling was
also corrected to wait for Core and workers before reopening ingress. No
gate hash was forced, duplicate experiment created or policy bypass applied.

## Verification And Limits

Immediately after start, public health returned 200 with the exact release,
healthy Redis and nine workers. The collector had a leader lease and consumer
group, with no pending entries or lag. The early report showed no observer
errors, replay failures or mutation attempts, but zero captured jobs and
capacity samples before the first five-minute sample. This proves startup,
not actual traffic capture, full coverage or review eligibility.

The subsequent check confirmed the first durable capacity sample and one
quorum slot. Early coverage was one of two expected slots (50%), with a
416-second maximum gap. No successful production completions or route
observations had yet occurred; errors and mutation attempts remained zero.
This verifies the sampler, not real-traffic capture or complete coverage.

Use the existing [runbook](../docs/VALIDATOR_SHADOW_RUNBOOK.md) `report` and
`transport` commands against this run and exact release. Preserve new snapshots
instead of overwriting the startup record. Evaluate successful-job correlation,
terminal coverage, capacity gaps, replay errors and absent alternatives before
making any recommendation. The comparison is same-model replica preference,
not a reconstruction of the production scheduler.

Rollback disables the observer through the reviewed Core rollout procedure,
preserving run data, the protected HMAC, additive schema and economic state.
Do not independently deploy another Core implementation into this frozen run.
No rollback of the live experiment was exercised after its successful start.
