# Validator Release Overlap: Production Evidence

## Artifact And Scope

- Production commit: `84fe0fd6202c2744c970dfeebd5103f1ad13f883`, PR130.
- Immutable release: `/home/aipg/releases/grid-core-84fe0fd6`.
- Cutover: September 7, 2026, 20:40:41-20:40:57 UTC.
- Rollback commit: `874f74071d9768d66d8031e2095ace10498dbd0d`.
- Rollback release: `/home/aipg/releases/grid-core-874f7407`.
- Alembic revision: `0039`, unchanged; no production migration ran.
- Approval: sole maintainer's supported-validator-beta deployment goal.
- Configuration: unchanged bytes; existing typed configuration gains capacity
  for seven exact reviewed upgrade tags plus the baseline. The active baseline
  remains preview.13, and the exact upgrades remain preview.15/.16/.17.

This deployment makes room for reviewed client releases while existing
operators migrate. It does not implicitly admit a newer tag, reset history,
verify an operator, start observation or authorize compensation.

## Verification

- Three new configuration checks failed with the old capacity; all 30 upgrade
  checks passed after the change.
- Real PostgreSQL 16 upgrade/history suite: 40 passed, no skips. This includes
  Python/SQL eligibility agreement, malformed versions and unchanged identity,
  signing wallet, account, review and qualification start across transitions.
- Full local Grid suite with disposable PostgreSQL URLs: 1,335 passed,
  eight environment-dependent skips. Local skips are not passing evidence.
- Required PR CI `34159501496` and merged-main CI `34159939981` passed,
  including backup/restore, schema parity and the actual PostgreSQL/Core/
  Auth.js/node compensation handoff. Full-history secret scan passed.
- Candidate dependencies installed as matching binary wheels from the existing
  hash-locked requirements; `pip check` passed. Dependency lock, Alembic tree,
  ORM schema, Nginx base and five service/timer units matched the prior release.
- Fresh backup: `grid-postgres-20260907T203651Z.dump`, with verified checksum,
  retained under `/var/lib/aipg-backup/validator-overlap-84fe0fd6/`.
  Restore into an isolated generated scratch database passed at `0039`;
  Alembic reported no new upgrade operations. Scratch database was removed.
- Cutover checked the existing Nginx configuration and service files, then
  atomically selected the release and restarted only the Grid API. No Nginx,
  timer or environment changes were needed.
- Private preservation checks passed for all 21 existing validator identity,
  account, signer, qualification and review bindings. All six compensation
  tables stayed empty. Operator consent, sender and shadow flags remained off.
- Worker payout and PostgreSQL backup timers remained active with unchanged
  enablement. No payout CLI was run by this deployment.
- API service: active/running, zero automatic restarts. Public `/health` and
  `/v1/status/network` both reported the exact new SHA and working Redis;
  ten workers had reconnected at 20:41 UTC.

## Remaining Gates

The seven fresh validator heartbeats at verification comprised three owned
preview.17 services and four external nodes on preview.13/.15. There were zero
finalized independence reviews. This is not a qualified paid cohort.

Preview.18 was tagged from reviewed validator master and its native release
workflow started separately. Do not count a tag or a green source build as
publication: verified artifacts, provenance, explicit Core admission and an
operator canary remain required. The Console consent page is separately live
at `28dbf5f`; Core's consent gate is still off. Budget approval and real human
consent remain separate from deploying these components.

Rollback currently requires only selecting the recorded previous code release
and restarting the API: the unchanged three-tag list fits its old capacity.
After expanding admission beyond three tags, restore a compatible reviewed
list before rolling back to that code. Never erase qualification or monetary
history to make rollback pass.
