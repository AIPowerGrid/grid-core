# sql_statements - legacy SQL migrations and maintenance

## Purpose

Legacy Horde SQL migration statements, stored procedures, cron jobs, and helper
queries used by the Flask runtime and bootstrap path.

## Ownership

- Versioned `*.txt` files - historical/manual SQL migrations.
- `queries.sql` - shared queries.
- `cron/` - pg_cron scheduling SQL.
- `stored_procedures/` - legacy Postgres stored procedures.
- `README.md` - SQL migration notes.

## Local Contracts

- These files target legacy Horde tables, not Grid v2 Alembic-managed tables,
  unless a file explicitly says otherwise.
- pg_cron is required by the legacy runtime; deploy/bootstrap grants privileges
  for it.
- Coordinate class/table changes in `horde/classes/` and DB helper changes in
  `horde/database/`.

## Work Guidance

- Use additive migrations where possible; legacy production data may exist.
- Include comments for irreversible or data-destructive SQL.
- Do not mix Alembic migration logic into these files.

## Verification

- Apply to a disposable Postgres DB when practical.
- At minimum, inspect SQL syntax and run `git diff --check` for docs-only edits.

## Child DOX Index

- `cron/` - scheduled maintenance SQL.
- `stored_procedures/` - legacy stored procedures.
