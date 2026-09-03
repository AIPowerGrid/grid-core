# Validator Shadow Runbook

## Status

Prepared procedure only. Do not execute it while Core reports fewer than three
recently participating, independently reviewed validator operator groups. Shadow
collection has no routing, reward, strike, bond, payout, or slashing authority.

Core `e1e4ad4c9eeb277f385a2359f3bc418917a7f0e1` and Alembic `0034` are
production-live with the observer disabled. The dark deploy passed a
production-backup restore/migration/drift proof; the shadow tables remained
empty and no route-event stream existed. A 2026-09-01 read-only gate run failed
only the three expected cohort checks. This does not authorize enabling the
collector.

The cohort baseline remains `v0.1.0-preview.13`. Do not publish a replacement
validator release merely to start this server-side run.

## Safety contract

- Deploy the reviewed Core release and migrate through Alembic `0034` with
  `VALIDATOR_SHADOW_OBSERVER_ENABLED=0` first. Migration `0032` creates the
  shadow records, `0033` adds the database-enforced single-running-run guard,
  and `0034` adds exact privacy-safe ledger correlation. `0034` intentionally
  fails if any observation already exists because the raw job id is unavailable
  for an honest backfill. `alembic current` must report `0034`.
- Use one stable, randomly generated 32+ character
  `VALIDATOR_SHADOW_ROUTE_HMAC_SECRET` for the entire run. Store it only through
  the production secret path and retain it with protected final-report evidence.
  Core cannot reproduce exact ledger coverage after this secret is lost or
  rotated and will fail the report closed.
- Every database mutation is preview-first. Apply with the exact UTC `--at` and
  gate/state hash from that preview. A changed gate or run row fails closed.
- Apply timestamps must remain within five minutes of Core's UTC clock, and a
  run cannot start before its draft was created. Core and the database permit
  at most one `running` shadow experiment.
- The implementation commit is the exact deployed Core commit, not a branch or
  abbreviated SHA. Core proves the executing runtime against that commit at
  draft creation, start, every durable append, close, and report generation.
  An unprovable build identity or mid-run release change stops collection and
  leaves outbox work pending instead of mixing implementations. The verification
  reference is the immutable GitHub Actions run or job URL that proves the
  candidate.
- Never expose the HMAC secret, database URL, Redis URL, validator identities,
  operator-control reviews, prompts, outputs, or route-event contents.
- A completed report can be reviewed; it cannot promote itself or change live
  behavior.

Production releases live under `/home/aipg/releases`, not `/opt`. Run every
command through the selected immutable release virtual environment:

```bash
PY=/home/aipg/releases/<release>/.venv/bin/python
TOOL=/home/aipg/releases/<release>/scripts/manage_validator_shadow_run.py
```

Do not point these variables at a mutable checkout.

The production environment file is root-readable and the service runs as
`aipg`. Load the environment as root, then enter an `aipg` shell with the target
user's home before using any command below:

```bash
sudo bash -c '
  set -a
  . /etc/aipg/grid.env
  set +a
  exec sudo -H -E -u aipg /bin/bash
'
test "$HOME" = /home/aipg
cd /home/aipg/releases/<release>
PY=$PWD/.venv/bin/python
TOOL=$PWD/scripts/manage_validator_shadow_run.py
```

Do not omit `-H`. Preserving root's `HOME` makes asyncpg inspect
`/root/.postgresql` and can fail before the gate is evaluated. Keep the
verification JSON readable by `aipg`; the file contains proof booleans and must
not contain database credentials or the route HMAC secret.

## 0. Finalize and recheck the independent cohort

The shadow gate counts verified operator groups, not candidates. Finalize each
candidate only after its public status reports a fresh heartbeat and the
maintainer has confirmed that the operator-control facts have not changed. Do
not run another `candidate` transition at the end of a qualification window:
that starts a new 72-hour window. Candidate re-entry is separately guarded by
an explicit restart flag.

Use the immutable production release and a non-sensitive review reference. The
tool's output includes the private opaque operator group, so inspect it only in
the protected production terminal and never paste it into GitHub, chat, or a
public incident report.

```bash
REVIEW=$PWD/scripts/review_validator_operator.py
VALIDATOR_ID=val_0123456789abcdef0123456789abcdef
REVIEW_REF=review:cohort-final-2026-09-08

$PY "$REVIEW" \
  --validator-id "$VALIDATOR_ID" \
  --action verify \
  --review-ref "$REVIEW_REF"
```

Require the preview to show all of the following:

- `current_status: candidate` and `proposed_status: verified`;
- `eligible_to_apply: true` and an empty `blocking_reasons` list;
- `time_ready`, `coverage_ready`, and `software_version_supported` all true;
- at least one completed probe and one authoritative attestation created after
  this preserved qualification observation window began; and
- `economic_effect: none`.

The activity check is qualification-window scoped. Lifetime evidence created
before the qualification observation started cannot satisfy it. If any field fails, keep
the node in candidate state and correct or observe the blocker; never edit the
timestamps, sample counters, or evidence rows.

Apply immediately with the exact digest from that preview. A heartbeat sample,
version change, status change, or concurrent review between the two commands
invalidates the digest and requires another preview.

```bash
$PY "$REVIEW" \
  --validator-id "$VALIDATOR_ID" \
  --action verify \
  --review-ref "$REVIEW_REF" \
  --apply \
  --expect-digest <preview-current-digest>
```

After each apply, check the redacted public status and the aggregate gate. The
public response must report `status: verified`, a fresh heartbeat, the frozen
cohort version, and `independent_vote_eligible: true`. It must not expose the
operator group or review reference.

```bash
curl -fsS \
  "https://api.aipowergrid.io/v1/validator/public/$VALIDATOR_ID"
GATE_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
$PY $TOOL gate \
  --verification-json /protected/shadow-verification.json \
  --at "$GATE_AT"
```

Repeat for three unrelated operator groups. A verified review expires after its
bounded review period; expiry removes eligibility. Starting a later review cycle
requires a deliberate candidate transition and a fresh qualification window,
not an in-place expiry extension. Do not prepare or start shadow collection
until the gate independently reports three recently participating verified
groups.

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
  --run-id shadow_2026_09_protocol_v4 \
  --implementation-commit <40-character-deployed-commit> \
  --verification-ref <immutable-grid-core-actions-run-or-job-url> \
  --verification-json /protected/shadow-verification.json \
  --at "$AT"
```

Inspect the complete policy, gate, commit, reference, and `start_gate_hash`. Apply
the same proposal with the exact timestamp and hash:

```bash
$PY $TOOL prepare \
  --run-id shadow_2026_09_protocol_v4 \
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
$PY $TOOL start --run-id shadow_2026_09_protocol_v4 --at "$START_AT"
```

Require `eligible_to_apply: true`, inspect every failed gate field, and apply
with the exact hash:

```bash
$PY $TOOL start \
  --run-id shadow_2026_09_protocol_v4 \
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
$PY $TOOL report --run-id shadow_2026_09_protocol_v4
```

Alert on collector lease loss, sustained stream backlog, observer errors,
capacity gaps, route-capture loss, replay failure, or any nonzero mutation count.
Do not repair bad evidence by editing rows or extending the frozen policy. Mark
the run failed and repeat with a new run ID after fixing the cause.

Any Core deployment during the window is a failed run unless it is the exact
same immutable implementation commit. Generate the final report from that same
release. The report fails review when it contains any observer error, no real
successful production ledger job, or any captured successful job commitment
that cannot be matched exactly to the run-window ledger.

Every report must state:

- `candidate_basis: post_dispatch_connected_compatible_replicas.v1`; and
- `counterfactual_scope: same-model replica preference, not exact production scheduler replay`.

This Grid uses pull-based dispatch. Do not present `would_change` as proof that
the production scheduler considered and rejected the hypothetical worker. The
sample can contain busy or prefetched replicas and can lead or lag dispatch by
the bounded registry-cache/background-capture window.

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
  --run-id shadow_2026_09_protocol_v4 \
  --status completed \
  --at "$END_AT"
```

Apply using the exact `current_run_state_hash`:

```bash
$PY $TOOL finish \
  --run-id shadow_2026_09_protocol_v4 \
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
