# horde/database - legacy DB helpers

## Purpose

Database setup, query helpers, and background thread helpers for the legacy
Flask/Horde runtime.

## Ownership

- `__init__.py`, `classes.py` - legacy DB/session setup and class imports.
- `functions.py`, `text_functions.py` - query/update helpers.
- `threads.py` - background cleanup and maintenance helpers.

## Local Contracts

- This area owns legacy Horde tables, not `grid_*` v2 tables.
- Changes may interact with SQL files in `sql_statements/` and PostgreSQL cron.
- Be careful with cleanup jobs that delete source images, waiting prompts, or
  worker state; they can affect live jobs.

## Work Guidance

- Keep long-running maintenance work out of request handlers.
- When changing cleanup/expiry behavior, verify R2/S3 key naming and lifecycle
  expectations.

## Verification

- Legacy tests under `tests/`; DB-backed behavior may require local services.

## Child DOX Index

- None - leaf.
