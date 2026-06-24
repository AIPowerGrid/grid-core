# docs/architecture - strategic architecture docs

## Purpose

Design and audit docs for Grid economics, demand-side billing, quality
validation, worker incentives, and trust boundaries.

## Ownership

- `GRID_ECONOMICS.md` - demand-side credits, identity, funding rails, developer
  incentives, and worker/protocol economics.
- `DEMAND_SIDE_AUDIT_BRIEF.md` - audit-oriented billing threat model,
  go-live blockers, and current live/dry-run posture.
- `PROOF_OF_QUALITY.md` - validator/probe/scoring model for measured worker and
  model quality.

## Local Contracts

- Keep the live/dry-run/stub status explicit. If a checklist marks an item done,
  code and tests must support that claim.
- Economics docs must distinguish demand billing from supply settlement.
- Identity guidance must remain aligned across docs: scoped bridge key plus
  signed user assertion supersedes raw trusted headers.
- Validator/slashing docs must not imply automatic slashing exists until
  enforcement and WorkerRegistry integration are wired and reviewed.

## Work Guidance

- Lead with invariants and threat models for money or trust docs.
- When an audit finds a blocker, record it as a gate with owner/component and
  verification expectations.
- Remove stale proposals once a newer accepted design replaces them.

## Verification

- `git diff --check`.
- For code-linked claims, inspect the referenced code path in the same turn.

## Child DOX Index

- None - leaf.
