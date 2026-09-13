# Validator Token-Limit Compatibility Deployment

## Selected Release

Core `d606e4d832df692ed1d90c4a6242c957a11d4d1e` replaced `93a21eec`
at **2026-09-13T02:44:58Z**. The selected tree exactly matches reviewed PR183
head `17ab6cd5255e7b042d5a1d9480209443daa6e06d`, whose required tests passed
in run `34732356509`, including PostgreSQL restore/schema checks and the pinned
Core/Console/node compensation integration. Only one validator service module,
its tests and documentation changed since the previous release. Dependencies,
billing/payout code, schema, Nginx base configuration and systemd assets did not.

The candidate was prepared as a detached immutable checkout with Python 3.12
and hash-locked binary wheels. `pip check` passed. A fresh production backup
was restored into a generated scratch database, migrated with this exact
candidate, and checked for schema drift. The proof passed at Alembic `0042`
with no new upgrade operations; the generated scratch database was removed.
No production migration or ledger rewrite was required.

Restore-proof SHA-256:
`41cf2543ac2b2afb13f8f0c3176bec45dc874295d448dd4398f9fc781f2c13ba`.

## Cutover And Controls

An exact-route maintenance overlay paused new public generations and targeted
validator probes while worker terminals remained reachable. The co-located MCP
service was stopped during the drain. Two consecutive checks found zero held
reservations and no pending or undelivered text/media queue work. The same
checks passed after stopping Core, before the atomic release-symlink switch.

Core and MCP restarted successfully; the temporary overlay was removed. Local
and public health reported the exact release SHA, healthy Redis and all eight
previously connected workers. Local network status independently reported the
new build. Unauthenticated public validator assignments returned `401`.

The production environment and existing worker-payout start-boundary drop-in
retained their pre-cutover hashes. The payout timer remained enabled and active;
this rollout neither invoked the sender nor changed its budget, valuation,
funding lineage or timing. Demand charging remained on. Validator compensation
collection/sending, media probes and shadow observation remained false, with
zero compensation campaigns. No operator independence review or qualification
reset was performed.

Queue-drain record SHA-256:
`a006c7b94e8f3e6c19f0c00be1659d89a521ae858d73a4ef83eb883f3ea513eb`.

## Scorer Verification

A read-only post-deployment check ran both scorers against 75 retained
token-limit assignment responses from the failed-group creation window in
`docs/architecture/VALIDATOR_FAILURE_AUDIT_2026-09-13.md`. It used each
assignment's own challenge, retained reasoning, finish reason and latency.

| Responses | Existing v1 | Counterfactual v2 |
| --- | --- | --- |
| 35 | Failed: partial terminal marker | Healthy |
| 25 | Failed: invalid repetition | Same failure |
| 7 | Failed: empty visible output | Same failure |
| 1 | Failed: terminal reason not length | Same failure |
| 7 | Healthy | Healthy |

The 35 corrected examples span nine groups. Healthy individual responses can
occur inside a group whose finalized outcome is failed. This is a retained-data
counterfactual, not fresh inference, a rescore of stored records or proof of
model identity. No historical verdict, signature or commitment was rewritten.

## September 13 Release Admission

Published validator preview.19 from `7bbda889` passed four-platform native
qualification, complete downloaded payload validation and exact-source GitHub
provenance. At **03:16:03 UTC**, Core's upgrade list gained only the exact .19
tag, retaining the .13 baseline and .15/.16/.17/.18 overlap. No source release,
schema, baseline, qualification record or payment policy changed.

A compare-and-swap update validated the old and new settings and changed only
`VALIDATOR_COHORT_UPGRADE_VERSIONS`. An exact-route maintenance overlay and
stopped MCP ingress allowed two quiet reservation/queue checks before the
restart, with another quiet check after Core stopped. Core/MCP and public
generation resumed; the overlay was removed. Public health reports `d606e4d8`,
healthy Redis and all eight workers. The running Core process environment
independently contains the exact five-version upgrade list.

The preexisting payout start-boundary drop-in kept its hash, and the timer
remained enabled and active. Validator compensation collection/sending, media
and shadow controls remain false. The configuration backup is private; all
other bytes in the environment were preserved. No funds were sent by this
operation. The prior v2 code backup/restore proof still applies: no code or
database changed in this admission-only restart.

## Remaining Rollout

Core now supports exact `text.token_limit.v2` assignments. Existing v1 evidence
and open v1 groups retain their original semantics. Published preview.18 does
not contain the node-side v2 scorer from validator PR112. Preview.19 is now
published and admitted, but a fresh live v2 assignment remains unverified.

Both remaining owned preview.17 services were separately upgraded to the
published preview.18 binary earlier that hour, preserving identity/configuration
and journals. Fresh reports `127667` and `127668` at 02:57 UTC subsequently
passed independent signature/binding and zero-economic-row checks. One of
these nodes then upgraded .18-to-.19, retaining identity/configuration and its
journal. Its first fresh report `127676` at 03:20:23 UTC also passed those
checks: a failed SmolLM instruction result, not a token-limit v2 canary.

The journal audit additionally found a node-side delivery gap for committed
empty responses, independent of the Core token-limit correction. The validator
repository's `EMPTY_COMPLETION_DELIVERY.md` records its reproduction, bounded
fix and not-yet-released status. Do not promote another public upgrade before
that correction is qualified. Neither finding authorizes compensation or
worker penalties. See `PREVIEW18_ROLLOUT.md` and `PREVIEW19_ROLLOUT.md` there for
the separate release checkpoints.

Private .19 canary aggregate SHA-256:
`471cdd548fdb9ac467316e58d2dd3b18bf5bfb622964a7911a1b5be4e3113273`.

Before rolling back to `93a21eec`, account for any newly issued v2 assignments
and pause that lane until they are drained. Preserve all additive schema,
funded-credit lineage, payout policies and the existing timer start boundary.
Never substitute a pre-funding-lineage or pre-frozen-payout sender.
