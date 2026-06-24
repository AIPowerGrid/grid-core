# docs/architecture-migration - migration planning

## Purpose

Planning docs for the transition from legacy Flask/Horde runtime to FastAPI,
Redis Streams, streaming LLM workers, and worker migration.

## Ownership

- `README.md` - migration doc index.
- `01-fastapi-migration.md` - Flask to FastAPI plan.
- `02-redis-streams.md` - queue/stream migration plan.
- `03-llm-streaming.md` - LLM streaming design.
- `04-worker-migration.md` - worker migration path.

## Local Contracts

- Migration docs describe intended direction; verify current code before treating
  them as live behavior.
- Keep compatibility and cutover risks explicit: public endpoints, worker
  protocol, database tables, and deployment ports.
- Do not delete old migration notes just because the code advanced; mark stale
  sections or link to the newer live docs.

## Work Guidance

- When implementation catches up to a plan, update the plan with current status
  and remaining risks.
- Cross-link to `grid_api/AGENTS.md`, `horde/AGENTS.md`, and `deploy/AGENTS.md`
  when migration affects runtime ownership.

## Verification

- `git diff --check`.

## Child DOX Index

- None - leaf.
