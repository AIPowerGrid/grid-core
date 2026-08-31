# Validator Preview Cohort Runbook

This is the maintainer procedure for moving an externally operated validator
from public registration through the 72-hour independent-operator
qualification. It does not enable validator rewards, routing influence,
strikes, bonds, or slashing.

The public operator instructions live in the validator repository's
`PREVIEW_COHORT.md`. This file owns the private Core-side review steps. Keep all
commands inside the selected immutable Core release with its configured Python
environment. Never paste production environment values into a command, ticket,
chat, or log.

From that release root, identify its Python without sourcing or printing the
production environment:

```bash
PY="$PWD/.venv/bin/python"
test -x "$PY"
```

## Intake Boundary

Accept only:

- the public opaque `val_*` validator id;
- operating system and CPU architecture;
- country or broad region;
- expected online hours;
- residential, datacenter, or cloud network class; and
- a private declaration of any common practical control with another node.

Never request an API key, private key, signature, account id, full wallet,
hostname, IP address, assignment payload, prompt, or worker response. Keep any
human contact details outside the Grid database. Core stores only an opaque
common-control group and a non-sensitive review reference.

## 1. Assign Common Control

One person or organization gets one `opg_*` group regardless of its number of
wallets, hosts, VMs, or cloud accounts. Reuse the same group for every node
under common practical control. Generate a new opaque value when the operator
has no existing group:

```bash
python -c 'import secrets; print("opg_" + secrets.token_urlsafe(12))'
```

Store the mapping from operator to opaque group only in the protected review
system. Do not encode a name, email, location, wallet, host, or ticket number in
the group id.

## 2. Start Qualification

Choose a non-sensitive review reference such as `cohort:2026q3:ticket-0001`.
Preview the transition first:

```bash
$PY scripts/review_validator_operator.py \
  --validator-id val_example \
  --action candidate \
  --operator-group opg_example01 \
  --review-ref cohort:2026q3:ticket-0001
```

Confirm the validator exists, its current state is expected, the proposed
status is `candidate`, the group is correct, and `economic_effect` is `none`.
Then apply only the exact fresh digest returned by that preview:

```bash
$PY scripts/review_validator_operator.py \
  --validator-id val_example \
  --action candidate \
  --operator-group opg_example01 \
  --review-ref cohort:2026q3:ticket-0001 \
  --expect-digest exact_preview_digest \
  --apply
```

Candidate application starts a new qualification clock and clears prior
heartbeat samples. Do not repeat it to repair a temporarily offline node; doing
so deliberately restarts the 72-hour window.

## 3. Monitor the 72-Hour Gate

The node must remain online for at least 72 hours and supply at least 80 percent
of the bounded heartbeat samples. Heartbeat freshness is checked again at
verification time. At least one completed assignment and one Core-accepted
authoritative attestation must also be created after the candidate clock starts;
historical evidence from before qualification does not count. Workload evidence
does not replace the time and heartbeat gates.

The operator can inspect its own safe progress with:

```bash
aipg-validator check --no-probe
```

The operator or maintainer should also confirm that the public status contract
matches the local node. Paste the public `val_*` id into
<https://aipowergrid.io/validate>, or query it directly:

```bash
curl -fsS https://api.aipowergrid.io/v1/validator/public/val_example
```

The public result may expose only the validator id, online state, last
heartbeat rounded to the minute, version, aggregate assignment/evidence counts,
redacted qualification progress, next action, and the fact that economic effect
is disabled. It must not expose accounts, wallets, signatures, operator groups,
review references, assignment content, evidence content, IPs, or operator
identity. Check the individual result before relying on aggregate network
counts; it catches stale versions and offline nodes without disclosing cohort
review data.

The maintainer can preview verification at any time:

```bash
$PY scripts/review_validator_operator.py \
  --validator-id val_example \
  --action verify \
  --review-ref cohort:2026q3:ticket-0001
```

Inspect `qualification`, `activity`, `eligible_to_apply`, and
`blocking_reasons`. An incomplete preview is read-only and reports every unmet
gate. Applying the same transition still fails closed until all blockers clear.

Production Core can also run the aggregate cohort watchdog with
`VALIDATOR_COHORT_MONITOR_ENABLED=1`. Every configured interval it evaluates
expired-assignment completion, accepted authoritative evidence, terminal probe
errors, validator disagreement, stale active/candidate nodes, software-version
drift from preview.13, and duplicate reviewed control groups. Alerts fire when
a condition first appears and when it recovers, rather than on every poll.
These are privacy-safe operational signals only: they contain counts, never
validator or control-group identifiers, and cannot change qualification,
routing, rewards, strikes, payouts, or slashing.

## 4. Verify Independence

Verification is a human common-control review, not an automatic claim that an
IP address, wallet, or VM is independent. Confirm the original disclosure is
still accurate and investigate obvious shared-control evidence outside Core.
Do not import that evidence into the Grid database.

When `eligible_to_apply` is true, rerun the preview immediately and apply its
fresh digest:

```bash
$PY scripts/review_validator_operator.py \
  --validator-id val_example \
  --action verify \
  --review-ref cohort:2026q3:ticket-0001 \
  --review-days 30

$PY scripts/review_validator_operator.py \
  --validator-id val_example \
  --action verify \
  --review-ref cohort:2026q3:ticket-0001 \
  --review-days 30 \
  --expect-digest exact_fresh_preview_digest \
  --apply
```

The default review lasts 30 days and can never exceed 90 days. Expiry removes
independent-vote eligibility automatically. Renewing through `candidate`
starts a fresh qualification rather than silently extending old trust.

## 5. Verify Public State

After approval, recheck the individual public result and then the public
aggregate:

```bash
curl -fsS https://api.aipowergrid.io/v1/status/network
```

`verified_independent` should increase only for a new control group, and
`participating_independent` only after recent assignment evidence. Public
responses must never expose validator ids, group ids, review references,
wallets, accounts, IPs, or operator identity. Distinct registrations and
reviewed independent operators remain separate counts.

## Rejection, Suspension, and Incidents

Use `--action reject` with the same preview/digest/apply sequence when the
independence claim cannot be accepted. Rejection removes independent
eligibility; it does not invent a strike or economic penalty. A cooperative
operator can run `aipg-validator suspend` to stop receiving new assignments.
Signing-wallet rotation uses `aipg-validator rotate` after the replacement
wallet is linked to the same canonical account.

If control is uncertain, a key may be compromised, or correlated behavior is
under investigation, remove the node from independent eligibility first. Do
not enable validator economics as an incident response shortcut. Preserve
signed evidence and non-sensitive review references for later dispute review.

## Exit Gate

Three distinct reviewed control groups completing qualification is the initial
production-cohort milestone. It is enough to assess independent onboarding,
shared-evidence handling, and basic disagreement behavior; it is not enough to
enable validator economics or routing authority.

The broader independent pilot is not proven until at least five distinct
reviewed control groups complete qualification and remain healthy through the
agreed observation window. Target ten operators for useful fault tolerance.
Keep validator rewards, routing influence, strikes, staking, and slashing
disabled through this cohort and through the later text/image/video evidence
pilots.
