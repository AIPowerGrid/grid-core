# Validator Anti-Gaming Gate

## Decision

Public generated canaries measure narrow protocol conformance and capability.
They do not measure general model quality and may not influence routing,
rewards, strikes, bonds, or slashing.

Random challenge values prevent exact response replay. They do not conceal a
public prompt family. A worker controls its client and backend and can classify
recognizable probes, answer them with a parser or specialist backend, and serve
ordinary jobs with something weaker.

Unsealed assignment polling also reveals the target, model, nonce, and challenge
before the validator invokes the probe. A colluding validator can use that lead
time to warn the target. The default-off sealed-assignment rollout replaces
those fields with a SHA-256 commitment and reveals them only in the terminal
probe response, after worker output. Nodes verify the disclosure before signing.
This closes advance API disclosure, not worker-side prompt recognition or
validator-controlled timing.

## Executable Baseline

`grid_api/routers/tests/test_validator_anti_gaming.py` runs hostile workers
against Core's real text challenge generator:

- a regex/template worker passes every current public challenge family without
  an AI model;
- an exact replay cache fails when Core rotates challenge values;
- a public-template classifier routes known probes to the template worker and
  ordinary-looking synthetic work to a deliberately weak backend;
- every resulting public-canary verdict remains explicitly ineligible for the
  `quality` evidence dimension.

CI requires this red-team contract. A change that makes the fixture classifier
miss a probe must update the hostile actor or provide measured evidence that
the traffic is genuinely less distinguishable. Deleting an attack test is not
evidence of hardening.

## Quality Lane Contract

A future quality-eligible audit must satisfy all of these conditions:

1. The workload is consented or synthetic and shaped like an ordinary request;
   private customer content is never silently reused.
2. Dispatch and terminal acknowledgments are indistinguishable from ordinary
   compensated jobs. No validator field, special job ID, zero-den marker, or
   timing shortcut crosses the worker boundary.
3. The candidate output is compared with rotating bonded references or a
   reviewed rubric. A single validator opinion is not objective truth.
4. The audit has strict per-validator, per-worker, hourly, and global budgets.
5. A held-out classifier evaluation measures recall, false-positive rate, and
   model-switching advantage. The corpus and labels used for the live gate are
   not published with the worker client.
6. Results remain evidence-only until independently controlled validators and
   the dispute path have passed the production pilot.

Sealed assignment support is required before this lane can be considered, but
it is not sufficient. Scheduler-owned production-shaped audits, ordinary worker
economics, and measured classifier resistance remain promotion gates.

## Promotion Gate

Do not set `quality_eligible=true` for generated templates. The first quality
lane may be promoted only after a supervised pilot demonstrates that a
worker-visible classifier has no material advantage over the agreed baseline,
while the same audits still detect a worker that serves weak ordinary output.
Record the corpus version, reference cohort, sample count, confidence interval,
and rollback decision with the release evidence.

Deterministic image fidelity is a separate evidence dimension. It may become
authoritative after independent reference agreement and media-specific gates;
it does not turn text canaries into quality evidence.
