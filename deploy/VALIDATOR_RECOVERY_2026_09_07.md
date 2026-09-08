# Validator Recovery Rollout

## Release Identity

- Environment: production Grid Core.
- Approving operator: the sole AIPG maintainer, under the active supported-beta
  rollout goal. No compensation budget or payment is approved by this deploy.
- Reviewed PR: [grid-core #120](https://github.com/AIPowerGrid/grid-core/pull/120).
- Commit: `508ca14fd61f2e80a23db72b9240006bf08a6470`.
- Immutable Git source tree: `f01c0a6481baa6e7dbfb4653c13ce387e5062fc3`.
- Dependency lock SHA-256:
  `9e6f362fbfccb4b3f80094531be9265d160ad7fcd5e63a9b3a72f933c288847d`.
- Database: Alembic `0034` -> `0035`; configuration schema and environment
  unchanged. No new runtime flag is required for collecting recent heartbeats.
- Deployment preparation began after merge at 2026-09-07 13:04 UTC; service
  cutover was 13:07:30 UTC; initial verification completed by 13:09 UTC.
- Code rollback target: `c42dd8d12556267f2c0edda02c1336cfceef6fbc`.
  Retain the additive columns during code rollback; do not downgrade or erase
  collected observations as an operational rollback.

## Verification

- PR checks and post-merge `grid-core tests` run `34125293760` passed.
- A separate local PostgreSQL 16.15 suite passed 34 recovery/review/concurrency
  tests with no skips. This includes actual migration downgrade/upgrade and
  same-bucket concurrent heartbeat updates, not SQLite-only proof.
- A fresh production backup was checksum-verified, restored into a generated
  scratch database, migrated with this exact release and checked with Alembic.
  PostgreSQL reported no schema drift at `0035`; the scratch database was
  removed. The protected backup remains on the host.
- The production migration completed before the atomic release switch.
  Dependencies were installed from the hash-locked binary-wheel resolution;
  `pip check` passed. Dependency, service and Nginx source files were unchanged;
  the live versioned Nginx base matched and `nginx -t` passed.
- A before/after aggregate digest over all 21 registrations' identity,
  original qualification start and review fields matched exactly. Raw records
  and private operator mappings are not published here.
- `/health` and `/v1/status/network` reported the exact deployed commit. All
  nine serving workers reconnected. Eight validators had started collecting
  real recent heartbeat buckets by the initial verification; no buckets were
  backfilled. The authenticated heartbeat path therefore ran on the new schema.
- Unauthenticated assignment access still returned HTTP 401. All four API
  workers completed startup; the initial log scan found no ERROR/Traceback
  lines, and the service restart counter was zero.
- Payout and backup timers retained their prior active state. Demand charging
  remained allowlisted. Validator economics remained `none`; no independence
  reviews, shadow run, penalty authority or payment was activated.

## Remaining Gates

This deploy starts recent-window collection; it does not manufacture a completed
72-hour recovery window. Initial post-cutover verification did not yet observe
a newly accepted signed attestation, so it is not a completed assignment canary
or a seven-day reliability result. Operator independence remained unverified
in the public aggregate. Finish the supported client updater/release, protected
operator reviews and fixed-duration pilot separately.
