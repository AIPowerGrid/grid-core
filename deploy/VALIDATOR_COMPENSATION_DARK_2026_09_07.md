# Validator Compensation Dark Deployment

## Release

- Authorization: sole maintainer's supported-validator-beta rollout goal.
- Immutable commit: `874f74071d9768d66d8031e2095ace10498dbd0d`.
- Production directory: `/home/aipg/releases/grid-core-874f7407`.
- Cutover: September 7, 2026, 19:57:16 through 19:57:32 UTC.
- Previous release: `3714a927733f61abeeeaf709867448cf8d54cb41`.
- Alembic: `0035` to `0039`; four additive compensation migrations.
- Configuration schema: `GridSettings` from the exact commit above. Production
  environment bytes were unchanged. New compensation flags default to false.

This deploys the reviewed allocation, recipient, nonce-aware sender and private
operator-consent implementations from PR124 through PR128. It does not start a
campaign, approve a budget, bind recipients or send validator payments.

## Preflight And Restore

PR run `34156611385` and merged-main run `34157044145` passed. They include real
PostgreSQL money-path checks, migration/schema parity and pinned Core/Console/
node consent integration. The latter uses synthetic earning/control records and
does not establish production operator independence or wallet-extension UX.

The candidate was cloned at the exact detached commit with a clean worktree.
Hash-locked binary wheels installed successfully and `pip check` passed. Its
dependency lock, Nginx base and all five versioned API/payout/backup units match
the prior release. The live Nginx base and units also match the candidate;
`nginx -t` passed. No unit, overlay or Nginx file was replaced.

A fresh protected production backup, timestamped `20260907T195426Z`, passed
checksum verification. Restoring it into a generated scratch database and
migrating with the candidate passed `0035` through `0039`, `alembic check` and
head equality. The proof tool removed its scratch database. Only afterward were
the same migrations applied to production, before selecting the new code.

## Verified Live

- Public health and network status report the exact release SHA. Network status
  was operational with all ten workers reconnected at 19:57:43 UTC.
- Before/after checks preserved all 21 validator identities, account/signing
  bindings, qualification starts and independence-review fields. Heartbeat
  collection was not reset or backfilled.
- All six compensation tables remain empty: campaigns, allocations, reviewed
  work, recipients, payments and consent requests.
- Compensation operator and sender flags and shadow observation remain off.
  Environment-byte equality also preserves pairing, media and demand settings.
- Public compensation returns `compensation_unavailable`; unauthenticated
  assignment access remains HTTP 401.
- API automatic restarts remain zero. The initial post-cutover journal scan
  found zero ERROR and zero Traceback lines among 174 lines.
- Payout and backup timers retained their prior active/enabled state. No payout
  CLI, validator sender or timer activation was invoked by this deployment.

The fleet snapshot reports eight fresh validators, 14 active registrations,
and zero verified-independent operators. These are different counts. This
deployment is not an authenticated human consent journey, a new native release,
a completed operator review, or a seven-day pilot result.

## Rollback And Remaining Gates

Retain additive schema on code rollback. Before any validator nonce is ever
bound, the previous code is compatible with the empty tables. After validator
transfers begin, never restore a worker allocator that ignores validator nonces:
retain the shared nonce-aware code/table and disable the validator sender.

Keep compensation off while the reviewed native release and live operator
journey are qualified. Console PR27 and validator PR107 are merged; validator
PR108 adds explicit four-platform compensation qualification. Account pairing
is still globally disabled. No client/source merge changes that production gate.

Finish three independent operator reviews without resetting existing history,
freeze the single bounded seven-day pilot, and obtain the owner's budget approval.
Compensation, shadow observation and worker-penalty authority are separate
decisions; this deployment grants none of them.
