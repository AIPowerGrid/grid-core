# Validator Probes and Economic Boundaries

This document describes the current code contracts. Deployment and activation
evidence belongs in the [demand launch record](../deploy/DEMAND_BILLING_LAUNCH_2026_09_08.md)
and validator rollout runbooks; source availability is not production proof.

## Retired coordinator sampler

The old `grid_api/services/probe.py` sampler is retired. Core no longer schedules
its background loop. The module retains only fail-closed compatibility entry
points: `run_once` and `_run_job` reject, and `probe_loop` logs retirement and
returns without dispatching. Old `GRID_PROBE_*` variables cannot enable it.

The demand audit found that its canaries used `job_queue.submit_job` as ordinary
text jobs, without a billing reservation or assignment-bound no-DEN terminal.
An evidence-only verdict therefore did not mean economically inert work:
successful completions could enter the ordinary worker ledger and legacy reward
pool. Production was observed with the old flag enabled on 2026-09-08. Retiring
the sampler does not delete or reprice that history; any historical exclusion
requires separate reconciliation before payout resumption.

The previous arithmetic/capital-city templates were also recognizable. A random
tag or temperature zero does not prove model identity, prevent probe-aware
switching, or make a static template a meaningful substitution detector.

## Registered-validator probes

The supported evidence path is `POST /v1/validator/probe/{assignment_id}`, owned
by `routers/validator.py`, `services/validators.py`, and the dedicated worker
terminal in `routers/worker_ws.py`.

- Core binds work to an active registration, assignment, nonce, and target.
- An atomic bounded lease prevents concurrent duplicate dispatch.
- The dedicated terminal runs before ordinary paid settlement and acknowledges
  zero DEN. It must not write ordinary completion or customer credit records.
- Attestations bind to the issued assignment and committed probe evidence.
- Missing capabilities, incomplete evidence, and reference disagreement must
  not be relabeled as cryptographic proof of dishonesty.
- Text/media fidelity modes have separate capability, reference, and rollout
  gates. A registered node or signed response does not establish independent
  ownership or grant routing, slashing, or payment authority.

See the [router contract](../grid_api/routers/AGENTS.md) and
[service contract](../grid_api/services/AGENTS.md) for exact current ownership.
Retiring the old coordinator sampler does not disable these routes, change
validator registration, or erase qualification evidence.

## Worker setup canaries

`services/worker_canaries.py` is a distinct operator onboarding path. It accepts
only a manager-bound worker credential, chooses the bounded challenge itself,
hard-targets that rig, and uses its dedicated no-DEN terminal. It is a
connectivity check, not a paid customer request or model-quality certificate.

## Compensated audits

Paying for audit execution requires the separate bounded
[compensated-audit design](architecture/PAID_VALIDATOR_AUDITS.md): reviewed
budget authority, durable holds, explicit terminal settlement, and the required
evidence/calibration gates. Do not obtain compensation by restoring the retired
ordinary-job sampler. Its old environment flag is not an economic authorization.

## Verification

- `pytest grid_api/services/tests/test_legacy_probe_retirement.py` proves legacy
  entry points cannot submit ordinary jobs even with the old flag enabled.
- Run the full `pytest grid_api/` suite to cover current validator, setup,
  worker terminal, and billing contracts. Required PostgreSQL CI remains a
  release gate; local skipped integration tests are not production proof.
- After deployment, inspect the actual release and process configuration,
  confirm current validator/setup paths still operate, and reconcile historical
  unreserved jobs separately. Do not infer that old accruals were removed merely
  because no new coordinator canaries can be created.
