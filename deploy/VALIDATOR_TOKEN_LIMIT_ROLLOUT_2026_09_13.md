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

## Combined Preview.20 Admission

Validator PR115's merged source `c73a284f155e86358f5348ab017747cd51d02e58`
was published as immutable `v0.1.0-preview.20` at **03:53:15 UTC**. Native
workflow `34736178270` passed all four builds, payload verification and all four
clean installs; container workflow `34736178277` also passed. All nine downloaded
release assets passed the exact manifest/archive/checksum check, and all eight
checksummed files passed exact-source/tag/workflow GitHub provenance verification
with self-hosted runners denied. Windows is unsigned and macOS unnotarized;
this remains a preview, not stable. The exact source suite ran 427 tests:
420 passed and seven skipped.

At **03:57:06 UTC**, the same bounded, drained admission procedure added only
preview.20 to the upgrade list. Baseline .13 and upgrades .15/.16/.17/.18/.19
remain admitted. The running process independently confirmed all six upgrade
tags and charging mode `on`. Core stayed on `d606e4d8`, with eight workers,
healthy Redis, unchanged payout controls and the temporary gate removed.
The typed old/new configuration comparison preserved every other setting,
including the four default-off validator economic/media/shadow controls.
No campaign, qualification reset, schema change or sender invocation occurred.

Three first-party nodes then rolled to the exact .20 Linux artifact one at a
time. Active registrations were confirmed at 03:58:46, 04:01:25 and 04:02:14
UTC. Each switch verified the previous release, candidate hash/size, non-root
version and offline decoder self-test, and preserved configuration and all
journal bytes before restart. Dead-letter counts at stop were 161, 161 and 141;
the first node also retained one pending assignment. No identity was recreated
and no dead letter was revived.

Fresh report `127694` at 03:58:50 UTC independently passed signature,
assignment/evidence binding and zero economic-row checks. It is a healthy Qwen
tool-chain result, not a v2 or committed-empty live canary. The other two
nodes' registrations are proven; their post-upgrade reports are not yet claimed.
The next fleet snapshot had nine recent heartbeats: three .20, one .18, one
.15 and four .13. Other nodes were not changed.

This combined node release also fixes skipped committed-empty text replies.
An offline replay of original sealed assignments and retained responses verified
114 completed-empty results, scored/signed them locally and exercised mocked
delivery; 47 unavailable responses remained unsigned. Those are historical
captures, not fresh jobs or live accepted reports, and are not revived for pay.

## Remaining Rollout

Core now supports exact `text.token_limit.v2` assignments. Existing v1 evidence
and open v1 groups retain their original semantics. Published preview.18 does
not contain the node-side v2 scorer from validator PR112. Preview.19 and the
combined preview.20 are published and admitted, but a fresh live v2 assignment
remains unverified.

Both remaining owned preview.17 services were separately upgraded to the
published preview.18 binary earlier that hour, preserving identity/configuration
and journals. Fresh reports `127667` and `127668` at 02:57 UTC subsequently
passed independent signature/binding and zero-economic-row checks. One of
these nodes then upgraded .18-to-.19, retaining identity/configuration and its
journal. Its first fresh report `127676` at 03:20:23 UTC also passed those
checks: a failed SmolLM instruction result, not a token-limit v2 canary.

The journal audit additionally found a node-side delivery gap for committed
empty responses, independent of the Core token-limit correction. The validator
repository's `EMPTY_COMPLETION_DELIVERY.md` records its reproduction and bounded
fix, now released in preview.20. Fresh owned-node v2 and committed-empty live
delivery remain distinct verification gates before public promotion.
Neither finding authorizes compensation or
worker penalties. See `PREVIEW18_ROLLOUT.md` and `PREVIEW19_ROLLOUT.md` there for
the separate release checkpoints.

Private .19 canary aggregate SHA-256:
`471cdd548fdb9ac467316e58d2dd3b18bf5bfb622964a7911a1b5be4e3113273`.

Private first .20 canary aggregate SHA-256:
`b9cddc554972c3e24a29a199e55237e347c930d3ce2ea33f55f1fbc1fe7b9de6`.

Before rolling back to `93a21eec`, account for any newly issued v2 assignments
and pause that lane until they are drained. Preserve all additive schema,
funded-credit lineage, payout policies and the existing timer start boundary.
Never substitute a pre-funding-lineage or pre-frozen-payout sender.
