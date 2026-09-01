# docs - architecture and runbooks

## Purpose

Durable architecture, economics, blockchain, migration, and audit documentation
for humans and agents.

## Ownership

- `architecture/` - strategic design: demand-side economics, Proof of Quality,
  text/media validation, and billing audit brief.
- `architecture-migration/` - Flask-to-FastAPI/Redis-stream/worker migration
  planning.
- `BLOCKCHAIN_INTEGRATION.md` - legacy/on-chain integration guide.
- `FUNDING_RAIL.md` - Base asset acceptance and x402 architecture.
- `FUNDING_CANARY_RUNBOOK.md` - dark deploy, real-money canary evidence, and
  rollback gates.
- `VALIDATOR_SHADOW_RUNBOOK.md` - exact cohort-finalization, preview/apply,
  transport-drain, rollback, and evidence procedure for the economically inert
  seven-day validator run.
- `V2.md` - v2 API/design notes.
- `architecture/DECENTRALIZATION_ROADMAP.md` - accepted post-preview Base
  validator and trusted-partner Core federation phases, event contract, and
  go-live gates.
- `architecture/PAID_VALIDATOR_AUDITS.md` - accepted default-off compensated
  audit rail: private server-side job binding, PostgreSQL budget reservations,
  ordinary worker economics, atomic settlement, and quality-promotion gates.
- `architecture/NETWORK_READINESS.md` - current implementation and rollout
  status for validator, worker-growth, economics, blockchain, and operations
  requirements.
- `architecture/VALIDATOR_ACCOUNT_PAIRING.md` - optional existing-account
  visibility links, two-party proof, expiry, privacy, recovery, and rollout
  requirements. No association is an authentication or economic identity.

## Local Contracts

- Docs must reflect current code posture. If a component is stubbed or ship-dark,
  say so plainly.
- Do not document a go-live command unless the command exists and has been tested.
- Keep Base/mainnet/testnet contract names and env vars consistent with code and
  deploy templates.
- Separate accepted decisions from rejected baselines. Remove stale
  contradictions instead of explaining around them.

## Work Guidance

- For audits, lead with invariants, threat model, live/dry-run posture, and
  blockers.
- For architecture, describe ownership boundaries and operational consequences,
  not just aspirational diagrams.
- When code changes endpoint behavior, billing, settlement, chain integration,
  or deployment, update the relevant doc in the same change.

## Verification

- Docs-only: `git diff --check`.
- For command/runbook docs, run or dry-run the command where safe and document
  any intentionally unverified step.

## Child DOX Index

- [architecture/AGENTS.md](architecture/AGENTS.md) - economics, proof-of-quality,
  and audit docs.
- [architecture-migration/AGENTS.md](architecture-migration/AGENTS.md) -
  migration planning docs.
