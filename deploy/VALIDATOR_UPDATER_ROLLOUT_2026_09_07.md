# Validator Updater Compatibility Rollout

## Immutable Release

- Approved by the sole AIPG maintainer under the supported-validator-beta goal.
- Reviewed Core PR: [#122](https://github.com/AIPowerGrid/grid-core/pull/122).
- Commit: `3714a927733f61abeeeaf709867448cf8d54cb41`.
- Production path: `/home/aipg/releases/grid-core-3714a927`.
- Code rollback: `508ca14fd61f2e80a23db72b9240006bf08a6470`.
- Cutover: 2026-09-07 15:10:08 UTC; candidate health verified at 15:10:20 UTC.
- Alembic remains `0035`; no schema or dependency-lock change.
- Configuration schema only expands the maximum exact reviewed upgrade list
  from two to three entries. Defaults and every other validation guard remain.

## Verification

The new three-tag settings cases failed before the limit change. Afterward,
41 focused checks passed on an isolated PostgreSQL 16.15 instance with no skips.
They cover real heartbeat writes, Python/SQL eligibility agreement, preserved
signer/account/qualification history, rejection of unlisted releases, rollback,
and refusal to enable shadow observation during version overlap. The local
scratch cluster was stopped and removed; no production test data was created.

PR run `34136006138` and post-merge run `34136429260` passed full Grid tests,
PostgreSQL migration/schema parity and backup/restore qualification. Security
checks passed. Production dependencies were installed as hash-locked binary
wheels; `pip check` passed. No production migration was pending or applied.

Preflight verified the candidate's exact clean Git state and compared the live
Nginx base, service units and dependency lock with the reviewed release. Those
files were already identical and were not overwritten. `nginx -t` passed.
The atomic symlink cutover included a bounded health check and previous-release
rollback on failure.

Both public health/status surfaces report the exact new commit; all nine
workers reconnected. Unauthorized assignment access remains HTTP 401. The API
restart counter stayed zero and the initial post-cutover log scan found no
ERROR/Traceback lines. Payout and backup timers remained active and enabled.

Across all 21 validator registrations, a before/after digest of identities,
qualification starts and private review fields matched exactly. Raw identities
and private control mappings are not included here. Eight nodes had completed
authenticated heartbeats after cutover at the initial follow-up; no newly
verified authoritative attestation had yet been observed. This is not a full
assignment canary or a seven-day reliability result.

## Version Admission And Release

At code cutover, the environment was byte-for-byte unchanged: baseline
preview.13, exact overlap preview.15 and preview.16, shadow observation off.
Demand charging remained allowlisted; validator economic effect remained none.

Validator PR #103 is merged as `aa35fa0a8ddea9087c25da4756bffe7e2e514458`.
The preview.17 tag points to that reviewed commit. Native release workflow
`34136400434` passed all four build, frozen handoff/recovery and clean-install
lanes and published the prerelease at 2026-09-07 15:15:53 UTC. These native
fixtures prove local handoff/recovery, not registered-node evidence delivery.

All nine downloaded release assets passed the exact tag/source/manifest and
archive verifier. The published manifest also passed GitHub attestation
verification pinned to the release workflow, tag ref and source/signer commit,
rejecting self-hosted provenance. The updater's own Sigstore verifier independently
accepted that manifest through the unauthenticated public attestation API.
Manifest SHA-256: `f2863b4575fadaf4e3dc7f2eb2247c4a21b5aa75367e5316858ef70f2422e9f7`.
Windows remains unsigned and macOS unnotarized; provenance does not substitute
for platform signing.

At 15:23:41 UTC the guarded production admission completed. Only
`VALIDATOR_COHORT_UPGRADE_VERSIONS` changed, from preview.15/.16 to
preview.15/.16/.17. A structured before/after comparison proved every other
environment setting identical. Baseline preview.13 is retained. Settings were
validated as the service user before atomic replacement, with a protected
environment backup, deployment lock and bounded restart/rollback health check.

Post-restart Core health reports the same reviewed commit; the API is active
with zero automatic restarts. All 21 stored identity, qualification-start and
private-review records have the same digest as before. Payout and backup timers
are still active. Public validator capabilities still report no economic effect,
rewards, staking, text fidelity or media fidelity. Shadow observation stays off.

The owned registered-node upgrade/pending-evidence canary and public download
promotion remain separate, incomplete steps. No public operator was upgraded by
changing Core's admission list. Never enable shadow observation during overlap.
Before reverting to the older two-tag Core implementation, restore the older
two-tag configuration as well. Stored qualification history is never reset,
backfilled or removed as part of upgrade or rollback.

Independent operator reviews, the fixed seven-day pilot, a separately approved
compensation budget and payment accounting remain open. No review, reward,
routing, strike or slashing authority was enabled by this rollout.
