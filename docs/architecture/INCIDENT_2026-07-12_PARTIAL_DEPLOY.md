# Incident: partial Core deploy caused chat outage (2026-07-12)

## Summary

Restarting `aipg-gridapi` to reload the TRELLIS recipe loaded a production
working tree whose Python files and PostgreSQL schema came from different stages
of the universal-account rollout. Text generation returned server errors until
two compatibility hotfixes were applied. The media recipe was not the cause;
the restart exposed already-present code/schema drift.

## Impact and recovery

- Chat completion requests failed before dispatch.
- The first failure was a call to the future three-argument account
  `authenticate()` contract while production still had the one-argument
  implementation.
- After restoring one-argument authentication on the chat route, legacy
  `waiting_prompts` inserts failed because two NOT NULL columns had no database
  defaults in the deployed schema.
- Production restored service by temporarily using the one-argument auth call
  and setting database defaults for `validated_backends` and
  `extra_slow_workers` to `false`.
- Real completions through automatic routing and multiple text models returned
  HTTP 200 after recovery.

## Semantics of the prompt defaults

The legacy Horde API continues to default `validated_backends=true` for legacy
requests. Grid-native text jobs use `validated_backends=false`: Grid worker
admission and routing are not the retired Horde backend-validation allowlist.
`extra_slow_workers=false` avoids opting Grid jobs into the legacy very-slow
worker pool. The Grid lightweight table mapping and insert now supply both
values explicitly; Alembic `0014` codifies the database fallback defaults.

## Root cause

Production was not a deployment of one reviewed Git commit. The checkout at
`aedb4afa` contained many modified tracked files, untracked routes/services, and
manual database changes. Restart behavior therefore depended on whichever
subset of files had most recently been copied to the host. Tests run against a
clean repository could not describe that runtime composition.

The Alembic version remained at `0006` even though the database already matched
the guarded `0007` through `0012` changes. Universal-account migration `0013`
was not present. The new identity-link and bridge-bootstrap endpoints were not
live at incident time.

## Reconciliation requirements

1. Preserve the live checkout and database as forensic backups; do not make it
   the next release artifact.
2. Push and identify one reviewed Core commit containing every intended runtime
   change, including the three-argument scoped authentication implementation.
3. Build and test a clean release directory from that commit. Never overlay
   selected files onto the running checkout.
4. Back up PostgreSQL, verify `0007` through `0012` parity, then advance Alembic
   from recorded `0006` through `0014`. The existing migrations guard objects
   that were applied manually.
5. Keep charging, promotional spending, and daily-free spending disabled.
6. Provision separate scoped bridge keys and run direct-key plus asserted-user
   canaries for text, Responses, Anthropic, image, video, and 3D before routing
   first-party applications to the release.
7. Switch the service atomically to the clean release, verify, and retain the
   previous release for rollback. A rollback must restore code and a
   schema-compatible state, not individual Python files.

## Preventive controls

- Deployment preflight fails when the target Git checkout is dirty.
- Schema migration precedes code whenever a hot path selects a new column.
- A restart is treated as a deployment event and runs the same canaries.
- Production hotfixes must be immediately represented by a reviewed commit and
  migration or explicitly reverted after recovery.
- Release verification records commit, Alembic version, service start time, and
  canary results together.
