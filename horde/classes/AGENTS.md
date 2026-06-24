# horde/classes - legacy ORM/domain objects

## Purpose

Legacy SQLAlchemy domain classes used by the Horde-compatible Flask runtime:
users, workers, waiting prompts, processing generations, styles, teams, filters,
interrogations, and related business objects.

## Ownership

- `base/` - shared base classes and common Horde entities.
- `stable/` - image/stable-diffusion-specific classes.
- `kobold/` - text/Kobold-specific classes.

## Local Contracts

- These classes map to legacy tables and cron/stored procedure assumptions. Check
  `sql_statements/` before changing columns or relationships.
- Do not use legacy kudos/worker objects as the source of truth for new v2
  settlement. v2 settlement reads `grid_ledger`.
- Avoid cross-importing new Grid services here unless a migration bridge is
  clearly documented.

## Work Guidance

- Keep ORM defaults and nullable behavior compatible with existing production
  rows.
- Add data migrations for schema changes; do not rely on implicit ORM changes.

## Verification

- Legacy tests under `tests/`.
- Full `pytest` when touching shared auth, users, or workers.

## Child DOX Index

- None - leaf.
