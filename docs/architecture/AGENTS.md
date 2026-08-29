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
  invariants, scorer-capability matching, scorecard privacy rules, and future
  economic gates.
- `VALIDATOR_ANTI_GAMING.md` - executable hostile-worker baseline, public-probe
  limitations, blind-audit contract, and the quality-evidence promotion gate.
- `VALIDATOR_ACCOUNT_PAIRING.md` - default-off, two-party association of an
  enrolled node with a human account, without identity merges, key transfer,
  payout changes, or trust grants. Core, Console and local-app implementations
  have real cross-repo HTTP coverage. Core `f51875ce` / `0030` and Console
  `db301013` are deployed dark; client release and live platform qualification
  remain separate gates. Never describe the disabled API as available to operators.
  A default-empty, expiring canonical-account pilot is deployed dark for
  supervised native qualification; it is not enabled by the dark deployment.
  A disposable Linux ARM64 pilot passed live association, restart recovery,
  both removal paths and unchanged non-pairing-state checks, then was disabled.
  Windows pairing, public client release and human desktop proof remain open.
- `PAID_VALIDATOR_AUDITS.md` - accepted, partially implemented design for
  bounded scheduler-owned audits. Schema, budgets, ordinary atomic payout, and
  recovery exist dark; scheduling, scoring, and classifier gates remain absent.
- `MEDIA_VALIDATION_V1.md` - accepted fail-closed image/video validation design:
  private challenges, cached bond eligibility, rotating references, Core object
  hashing, validator fetch defenses, dark validator-side modality scoring, and
  rollout gates. Core video issuance/execution remains disabled.
- `DECENTRALIZATION_ROADMAP.md` - accepted sequence from independently proven
  validator quorum through Base evidence/reward contracts and signed-event
  trusted-partner Core federation.
- `NETWORK_READINESS.md` - evidence-linked status ledger for the 35 network
  backlog items; keeps source, production, public-release, and external-operator
  proof distinct.
- `WORKER_PROFILE_V1.md` - signed worker installation profiles, ACE-Step audio
  data flow, identity/privacy boundaries, and go-live gates.
- `ACE_STEP_AUDIO_CONTROLS.md` - governed ACE-Step control surface, quality
  calibration gate, and the boundary between public creative controls and
  private runtime controls.
- `UNIVERSAL_ACCOUNTS.md` - canonical identity, frontend assertion, linking,
  merge, and three-pocket credit contracts.
- `SERVICE_ACCOUNTS.md` - bounded backend principals, native user-token
  exchange, provisioning commands, ceilings, and rollout order.
- `REMOTE_MCP_AUTH.md` - dark OAuth 2.1/PKCE contract for a remote MCP resource,
  Console consent boundary, least-privilege introspection service, and rollout
  gates.
- `INCIDENT_2026-07-12_PARTIAL_DEPLOY.md` - chat outage caused by restarting a
  code/schema-divergent production checkout; reconciliation requirements.

## Local Contracts

- Keep the live/dry-run/stub status explicit. If a checklist marks an item done,
  code and tests must support that claim.
- Economics docs must distinguish demand billing from supply settlement.
- Identity guidance must remain aligned across docs: Core-verified Google/SIWE
  proofs issue short native user tokens; bounded service accounts may exchange
  only their namespaced app subjects. One-use assertions are app-only legacy
  transport, never authority for global Google or wallet identities.
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
