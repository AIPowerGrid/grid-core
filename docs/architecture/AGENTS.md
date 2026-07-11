# docs/architecture - strategic architecture docs

## Purpose

Design and audit docs for Grid economics, demand-side billing, quality
validation, worker incentives, and trust boundaries.

## Ownership

- `GRID_ECONOMICS.md` - demand-side credits, identity, funding rails, developer
  incentives, and worker/protocol economics.
- `DEMAND_SIDE_AUDIT_BRIEF.md` - audit-oriented billing threat model,
  go-live blockers, and current live/dry-run posture.
- `RECIPE_DISPATCH.md` - how to add a media workflow: recipe = governed ComfyUI
  graph (`_grid` node map), the importer CLI, authoring steps, dispatch flow, and
  the coordinator/worker split. Start here to add an image/video model.
- `PROOF_OF_QUALITY.md` - validator/probe/scoring model for measured worker and
  model quality.
- `VALIDATOR_V0.md` - core-side validator V0 endpoint contract, evidence-only
  invariants, scorecard privacy rules, and future economic gates.
- `UNIVERSAL_ACCOUNTS.md` - canonical identity, frontend assertion, linking,
  merge, and three-pocket credit contracts.

## Local Contracts

- Keep the live/dry-run/stub status explicit. If a checklist marks an item done,
  code and tests must support that claim.
- Economics docs must distinguish demand billing from supply settlement.
- Identity guidance must remain aligned across docs: scoped bridge key plus
  signed user assertion supersedes raw trusted headers.
- Validator/slashing docs must not imply automatic slashing exists until
  enforcement and WorkerRegistry integration are wired and reviewed.
- Validator/fidelity docs must separate reproducible workflow certification
  from product policies such as NFT minting or marketplace eligibility.

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
