# Validator Shadow Runbook

## Status

Prepared procedure only. Do not execute it while Core reports fewer than three
recently participating, independently reviewed validator operator groups. Shadow
collection has no routing, reward, strike, bond, payout, or slashing authority.

The cohort baseline remains `v0.1.0-preview.13`. Do not publish a replacement
validator release merely to start this server-side run.

## Safety contract

- Deploy the reviewed Core release and migration with
  `VALIDATOR_SHADOW_OBSERVER_ENABLED=0` first.
- Use one stable, randomly generated 32+ character
  `VALIDATOR_SHADOW_ROUTE_HMAC_SECRET` for the entire run. Store it only through
  the production secret path.
- Every database mutation is preview-first. Apply with the exact UTC `--at` and
  gate/state hash from that preview. A changed gate or run row fails closed.
- Apply timestamps must remain within five minutes of Core's UTC clock, and a
  run cannot start before its draft was created. Core and the database permit
  at most one `running` shadow experiment.
- The implementation commit is the exact deployed Core commit, not a branch or
  abbreviated SHA. The verification reference is the immutable GitHub Actions
  run or job URL that proves the candidate.
- Never expose the HMAC secret, database URL, Redis URL, validator identities,
  operator-control reviews, prompts, outputs, or route-event contents.
- A completed report can be reviewed; it cannot promote itself or change live
  behavior.

Run every command through the selected immutable release virtual environment:

```bash
PY=/opt/aipg/releases/<release>/.venv/bin/python
TOOL=/opt/aipg/releases/<release>/scripts/manage_validator_shadow_run.py
```

Do not point these variables at a mutable checkout.

## 1. Verification evidence

Create a protected local JSON file containing exactly:

```json
{
  "postgres_migration_verified": true,
  "postgres_concurrency_verified": true,
  "replay_verified": true,
  "no_side_effect_verified": true
}
```

Set a value to `true` only when the immutable candidate has the corresponding
artifact. The tool validates shape and types, but a human must verify the linked
evidence honestly.

## 2. Read the gate

Use a fixed UTC timestamp so a later apply can prove it is acting on the exact
preview:

```bash
AT=2026-09-08T16:00:00Z
$PY $TOOL gate --verification-json /protected/shadow-verification.json --at "$AT"
```

Required output includes all of the following:

- `verified_independent_operators >= 3`;
- `participating_independent_operators >= 3`;
- at least one finalized independent probe group;
- no unresolved critical cohort incident;
- all four verification booleans true; and
- `routing_effect` and `economic_effect` both `none`.

Do not continue merely because the aggregate validator count is three. The gate
counts independently reviewed control groups, not registrations or machines.

## 3. Prepare an inert draft

A draft may be created before the operator gate is complete. It enables nothing.
Preview first:

```bash
$PY $TOOL prepare \
  --run-id shadow_2026_09_protocol_v2 \
  --implementation-commit <40-character-deployed-commit> \
  --verification-ref <immutable-grid-core-actions-run-or-job-url> \
  --verification-json /protected/shadow-verification.json \
  --at "$AT"
```

Inspect the complete policy, gate, commit, reference, and `start_gate_hash`. Apply
the same proposal with the exact timestamp and hash:

```bash
$PY $TOOL prepare \
  --run-id shadow_2026_09_protocol_v2 \
  --implementation-commit <40-character-deployed-commit> \
  --verification-ref <immutable-grid-core-actions-run-or-job-url> \
  --verification-json /protected/shadow-verification.json \
  --at "$AT" \
  --apply \
  --expect-gate-hash <preview-start-gate-hash>
```

## 4. Enable the inert collector

Only after the deployed release, migration rollback proof, secret injection, and
three-operator gate are verified:

1. set `VALIDATOR_SHADOW_ROUTE_HMAC_SECRET` through the secret path;
2. set `VALIDATOR_SHADOW_OBSERVER_ENABLED=1`;
3. restart Core through the normal atomic deployment path;
4. verify ordinary text, passthrough, image, video, audio, credits, settlement,
   payouts, and validator assignments are unchanged; and
5. read transport health:

```bash
$PY $TOOL transport
```

The collector group and a positive leader-lease TTL must appear. The start tool
enforces both. A nonzero backlog during normal traffic is acceptable only when
it drains; sustained growth is a failed canary. Roll back the flag on any
live-path regression.

## 5. Start exactly seven days

Take a fresh preview with a new fixed UTC time:

```bash
START_AT=2026-09-08T17:00:00Z
$PY $TOOL start --run-id shadow_2026_09_protocol_v2 --at "$START_AT"
```

Require `eligible_to_apply: true`, inspect every failed gate field, and apply
with the exact hash:

```bash
$PY $TOOL start \
  --run-id shadow_2026_09_protocol_v2 \
  --at "$START_AT" \
  --apply \
  --expect-gate-hash <preview-start-gate-hash>
```

Record the returned `scheduled_end`. Do not edit the policy or HMAC secret during
the run.

## 6. Monitor without authority

At least daily, archive these aggregate outputs:

```bash
$PY $TOOL transport
$PY $TOOL report --run-id shadow_2026_09_protocol_v2
```

Alert on collector lease loss, sustained stream backlog, observer errors,
capacity gaps, route-capture loss, replay failure, or any nonzero mutation count.
Do not repair bad evidence by editing rows or extending the frozen policy. Mark
the run failed and repeat with a new run ID after fixing the cause.

## 7. Drain and close

After `scheduled_end`, leave the run in `running` state for at least five
minutes while the bounded producer and collector finish events observed inside
the window. This deliberately exceeds the five-second producer timeout and the
collector loop interval. New events outside the window are discarded. Poll:

```bash
$PY $TOOL transport
```

Proceed only after the capture grace has elapsed and `drained: true`; the
completion tool enforces both. Then preview completion at a fixed UTC time:

```bash
END_AT=2026-09-15T17:05:00Z
$PY $TOOL finish \
  --run-id shadow_2026_09_protocol_v2 \
  --status completed \
  --at "$END_AT"
```

Apply using the exact `current_run_state_hash`:

```bash
$PY $TOOL finish \
  --run-id shadow_2026_09_protocol_v2 \
  --status completed \
  --at "$END_AT" \
  --apply \
  --expect-state-hash <preview-current-run-state-hash>
```

Generate and archive the final report. `review_eligible: true` means the evidence
package is complete enough for human review; it is not authorization to enable
routing weight or rewards. After the report is archived, set
`VALIDATOR_SHADOW_OBSERVER_ENABLED=0` through the normal atomic deployment path
and verify the route-event stream remains empty. Retain the run's HMAC secret in
protected evidence storage; do not reuse it for another run.

## Rollback

For a collector or live-path incident, first disable
`VALIDATOR_SHADOW_OBSERVER_ENABLED` and atomically redeploy the last known-good
release. Then preview and apply `finish --status failed` against the affected run.
Do not rotate or delete evidence, reuse the run ID, or convert a failed run to
completed.
