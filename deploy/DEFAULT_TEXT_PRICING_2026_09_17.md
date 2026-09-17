# Standard Text Tariff: September 17, 2026

## Release

Production selects `e3741222ab6bbe2a537376085116d4eafd1bb37f`, from PR #209.
Core restarted at 22:25:11 UTC; public checks passed at 22:25:28 UTC with
healthy Redis and all seven preflight workers reconnected. This supersedes
`3384c223` without a production schema migration or billing-policy toggle.

New nonempty concrete text model names without an explicit price resolve to
USD0.075 per million input tokens and USD0.30 per million output tokens.
Explicit prices and aliases take precedence, including cheaper models.
Explicit zero-text-rate entries, media-only entries, blank names and unresolved
`auto` names do not acquire a text tariff through this fallback. Auto routing
resolves a concrete model before reserving its cost.

The tariff is customer pricing, not model registration, quality approval or
reward weighting. A model still needs an available compatible worker. Unknown
image, video, audio and 3D models remain unpriced and denied in charging mode.
No worker-controlled price or inferred model-size reward is introduced.

## Accounting and Discovery

Quotes and reservation snapshots use the same effective rate. Holds retain
their original rates if a price override changes later; completion/refund and
duplicate-terminal behavior are unchanged. The rate book is `2026-09-17-a`.

- `/v1/pricing` publishes `price_book.default_text`.
- `/v1/models` and concrete model detail expose optional `pricing` metadata.
- `/v1/status/models` exposes effective pricing on text rows.
- Metadata includes USD input/output rates, `model` or `default` source and
  rate-book version. An unresolved auto choice has no fixed advertised rate.

Chat's optional display of this metadata is a separate frontend release. Core
activation does not establish that its model-picker tooltip has deployed.

## Verification

- [Required PR CI](https://github.com/AIPowerGrid/grid-core/actions/runs/35278857067)
  and [exact-main CI](https://github.com/AIPowerGrid/grid-core/actions/runs/35280615562)
  passed. The merge tree exactly matches the reviewed PR head.
- Both full CI suites: 2,154 passed, 12 skipped, with PostgreSQL16 configured
  for credit, payout, validator and shadow tests. Redis collector proof,
  backup/restored migration, schema parity and Core/Console/node handoff passed.
- Regression coverage includes unknown-model nonzero quotes and holds,
  insufficient balance, service ceilings, frozen rates across an override,
  settle/release/duplicate terminals, explicit-price precedence and media
  isolation. The new lifecycle fixture uses SQLite; the existing concurrency
  invariants are separately exercised by PostgreSQL CI.
- Local Core suite: 1,670 passed, 495 skipped before the last quote regression;
  the final eight-test quote module also passed. Local skips are not DB proof.

## Rollout Controls

A fresh production backup restored into a scratch database and passed migration
and parity checks against the exact candidate. Runtime dependencies installed
from hash-locked wheels. New generation/probes were temporarily gated and MCP
stopped while queues/holds drained. Exact release readiness and all seven worker
connections were required before ingress reopened. Six Core processes matched
the protected configuration. Alembic remains `0042`.

Charging stayed on and daily free spending stayed enabled. The environment,
apart from an existing build-commit field if present, was preserved. Compensation
contract hashes and payout timer/drop-in state were unchanged. Both queues were
quiet and there were zero holds. Read-only balance reconciliation found no
negative balances, mismatched accounts, stale holds or invalid pocket splits.
There were zero active pinned shadow runs before and after deployment; no run
was closed/started and no observation HMAC was rotated. Protected client GPT
services, model backends and worker identities were untouched. Core and MCP
were active with zero restart-loop counts after reopening.

The loaded runtime quotes a previously unlisted text name at 375 micro-USD for
1,000 input plus 1,000 output tokens; the same name remains unpriced for images.
Public pricing and both model lists expose the new metadata, and the existing
Qwen rate stayed unchanged. Seven unauthenticated empty-body generation checks
retain their prior 401/422 responses. These are no-dispatch checks, not a live
new-model completion or a full authorization audit; new-model reserve/settle/
release behavior is covered by automated fixtures. No customer test spend or
balance adjustment was made for this rollout.

## Rollback

The previous immutable `3384c223` release and protected original environment
are retained. Gate and drain before any rollback, preserving payout controls
and reservation/ledger history. A rollback restores the missing-price rejection
for newly advertised text names; it must not disable charging to work around it.
