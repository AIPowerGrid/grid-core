# Validator V0 Core Contract

## Status

V0 is an evidence-only preview. Core can register validators, issue shared
targeted text probe groups, verify signed evidence, aggregate distinct-validator
3-of-5 quorum, and expose scorecards. No
validator evidence changes routing, worker health, den, payouts, rewards, bonds,
credits, strikes, or slashing.

The quorum mechanism is implemented but has no economic authority. It proves
distinct registered Grid accounts signed votes over one shared challenge; it
does not prove those accounts are independently controlled. Independent
operators, correlated-control defenses, dispute windows, and adversarial
operation remain rollout gates.

## Identity

1. A user links a verified Base wallet to a canonical Grid account.
2. The Console issues a dedicated key with exactly:
   `validator.assignments`, `validator.probe`, `validator.attest`, and
   `validator.read`.
3. The node signs a canonical registration payload with that linked wallet.
4. Core verifies the signature, timestamp, account/wallet binding, software
   version, and declared capabilities before activating `grid_validators`.
5. Assignments and authoritative attestations carry the registered
   `validator_id` and `probe_group_id`; one canonical account may register one
   validator, and one validator may submit one authoritative vote per group.

The signing key proves control of the registered evidence identity. It does not
prove validator stake, independent operation, correct execution, or future
economic eligibility.

## Endpoint Contract

| Endpoint | Scope | Purpose |
|---|---|---|
| `GET /v1/validator/capabilities` | public | discover preview features and economic boundary |
| `POST /v1/validator/register` | `validator.attest` | verify and activate linked signing identity |
| `GET /v1/validator/registration` | `validator.read` | inspect active registration |
| `POST /v1/validator/heartbeat` | `validator.attest` | refresh version, capabilities, and liveness |
| `GET /v1/validator/assignments` | `validator.assignments` | receive short-lived, node-bound work |
| `POST /v1/validator/probe/{assignment_id}` | `validator.probe` | hard-target the assigned worker |
| `POST /v1/validator/attest` | `validator.attest` | submit signed assignment evidence |
| `GET /v1/validator/scorecards` | `validator.read` | read redacted aggregates |
| `GET /v1/validator/assignments/health` | `validator.read` | inspect assignment workflow health |
| `GET /v1/validator/workers` | `validator.read` | read inventory only |

Missing registration, assignment, probe, or attestation support fails closed.
The public inference API and worker inventory are never alternate probe paths.

## Evidence Invariants

Authoritative evidence must match all of:

- active registration and registered signing wallet
- assignment owner (`validator_id` and canonical account)
- shared probe group and one-vote-per-validator membership
- Grid-issued assignment id and nonce within the attestation window
- assigned worker, model, modality, and capability
- evidence hash returned by the hard-targeted probe
- valid EIP-191 signature over the canonical payload

New probes stop at assignment expiry. A completed probe may deliver its signed
attestation during a bounded post-expiry grace window (30 minutes by default),
so a brief Core or network outage does not silently erase valid evidence.

Assignment responses reveal the prompt and an expected-answer SHA-256
commitment, never Core's plaintext expected answer. Probe responses reveal the
committed output and transport hashes, never Core's private verdict. Each node
scores that output locally before signing.

The targeted probe is isolated from customer economics: it does not reserve or
settle demand credits, award den, create a payout ledger completion, or apply a
worker strike.

## Privacy

Scorecards expose aggregate verdict counts and health only. They must not expose
raw prompts or outputs, expected answers, nonces, signatures, canonical account
IDs, validator identities, private challenge policies, or reference outputs.

## Schema And Deployment

Alembic `0020` creates `grid_validators`, adds nullable registration attribution
to existing assignments and attestations, and enforces one attestation per
assignment/validator. Alembic `0021` adds atomic probe attempt counters and
reclaimable leases, preventing concurrent replay from dispatching duplicate
free inference. Alembic `0022` adds shared probe groups, one validator per
canonical account, and DB-enforced one-assignment/one-attestation membership per
validator and group. Apply all migrations before deploying shared-quorum Core
code. Existing legacy evidence may remain unbound and must never be upgraded to
authoritative by inference.

## Next Authority Gate

Before evidence can affect routing or rewards, the network must prove multiple
independently operated nodes in production, add self-validation and
correlated-operator controls, define dispute windows, and make evidence
replayable. Slashing requires a separate objective-fraud policy and contract
review after those controls are proven.
