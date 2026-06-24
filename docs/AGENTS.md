# docs - architecture and runbooks

## Purpose

Durable architecture, economics, blockchain, migration, and audit documentation
for humans and agents.

## Ownership

- `architecture/` - strategic design: demand-side economics, Proof of Quality,
  billing audit brief.
- `architecture-migration/` - Flask-to-FastAPI/Redis-stream/worker migration
  planning.
- `BLOCKCHAIN_INTEGRATION.md` - legacy/on-chain integration guide.
- `V2.md` - v2 API/design notes.

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
