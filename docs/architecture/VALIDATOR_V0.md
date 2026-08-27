# Validator V0 Core Contract

## Status

V0 is an evidence-only preview. Core can register validators, issue targeted
text capability batches, verify signed evidence, aggregate distinct-validator
3-of-5 quorum, and expose scorecards. No
validator evidence changes routing, worker health, den, payouts, rewards, bonds,
credits, strikes, or slashing.

`auto` model and replica scores explicitly exclude `grid_validator_attestations`;
they use curated tiers and Grid-measured throughput/latency only. Public-template
canaries therefore cannot steer production traffic through a side channel.

The quorum mechanism is implemented but has no economic authority. It proves
distinct registered Grid accounts signed votes about one worker/capability
batch; each new v8 assignment has a separately randomized challenge. It does
not prove those accounts are independently controlled or establish general
model quality. Independent
operators, correlated-control defenses, dispute windows, and adversarial
operation remain rollout gates.

Production runs Core `e18b38f9` at Alembic `0026` with sealed assignment
polling enabled. Three first-party nodes run published
`v0.1.0-preview.2` commit `1472677d`. A 2026-08-27 canary proved that the poll
withheld target/model/nonce/challenge data, all three terminal disclosures
matched their seals, and one tool-chain group reached healthy 3-of-5 quorum
without creating credit, reservation, den, or worker-ledger rows. This remains
first-party protocol evidence, not independent operator proof.

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
6. The operator may self-suspend with a fresh current-wallet signature. A lost
   or replaced signing key is rotated only after the same canonical account
   links and signs with a different replacement wallet; the stable validator
   ID and its historical attribution do not change.

The signing key proves control of the registered evidence identity. It does not
prove validator stake, independent operation, correct execution, or future
economic eligibility.

## Endpoint Contract

| Endpoint | Scope | Purpose |
|---|---|---|
| `GET /v1/validator/capabilities` | public | discover preview features and economic boundary |
| `POST /v1/validator/register` | `validator.attest` | verify and activate linked signing identity |
| `GET /v1/validator/registration` | `validator.read` | inspect active or self-suspended registration |
| `POST /v1/validator/suspend` | `validator.attest` | stop new work with a current-wallet signature |
| `POST /v1/validator/rotate` | `validator.attest` | bind the stable validator ID to a newly linked, newly signed wallet |
| `POST /v1/validator/heartbeat` | `validator.attest` | refresh version, capabilities, and liveness |
| `GET /v1/validator/assignments` | `validator.assignments` | receive short-lived, node-bound work |
| `POST /v1/validator/probe/{assignment_id}` | `validator.probe` | hard-target the assigned worker |
| `POST /v1/validator/attest` | `validator.attest` | submit signed assignment evidence |
| `GET /v1/validator/scorecards` | `validator.read` | read redacted aggregates |
| `GET /v1/validator/assignments/health` | `validator.read` | inspect assignment workflow and aggregate network health |
| `GET /v1/validator/workers` | `validator.read` | read inventory only |

Missing registration, assignment, probe, or attestation support fails closed.
The public inference API and worker inventory are never alternate probe paths.
Self-suspension is reversible by a fresh signed registration from the same
wallet. Maintainer revocation is not: registration, suspension, and rotation
all reject a revoked identity. Rotation does not rewrite historical evidence,
and old in-flight assignments retain their old wallet binding until they expire.

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
Core stores a JSON-safe synthetic probe-result envelope (maximum 512 KiB) when
it marks the probe completed. Until the assigned validator submits its
authoritative vote, assignment polling returns that unfinished delivery and a
repeat probe request replays the stored envelope with `replayed: true`. Replay
is owner-bound, does not contact the worker, and does not consume another probe
attempt. Core refuses to mark the probe completed if the replay envelope cannot
be committed.

Assignment responses reveal the prompt and an expected-answer SHA-256
commitment, never Core's plaintext expected answer. Probe responses reveal the
committed output and transport hashes, never Core's private verdict. Each node
scores that output locally before signing.

Candidate text families are randomized exact instruction, generated
arithmetic, strict JSON object compliance, calibrated 4K/16K/32K context retrieval,
generated multistep integer logic, one exact randomized function call, one
exact two-stage tool-call chain, and randomized stop-sequence compliance. The
validator registration must advertise the matching scorer capability before it
can join that probe group, and context lanes are only assigned when the target
worker advertises conservative context headroom. Legacy
`text.basic.v1` registrations are compatible only with echo/arithmetic. This
prevents an older binary from turning an unsupported challenge into a false
worker failure.

Scorecards classify these results as availability, protocol conformance, or
narrow capability evidence. Public generated templates are recognizable even
when their values are random, so no current text canary is quality-eligible and
none proves the exact model behind a worker.

For objective text lanes, Core independently computes a private verdict and
scorecards report how many signed validator votes matched or disagreed with it.
Disagreement remains visible signed evidence; it is not silently relabeled as a
Core-verified fact. Media fidelity and preview-only rows remain explicitly
`validator_opinion` because Core commits transport witnesses but does not run
the node's local pHash/decode scorer.

The targeted probe is isolated from customer economics: it does not reserve or
settle demand credits, award den, create a payout ledger completion, or apply a
worker strike.

It still consumes worker capacity. Core therefore creates at most one new text
probe group per worker/model per configured interval (one hour by default,
never less than five minutes). A v8 group stores a bounded generator/capability
envelope and each assignment stores its concrete randomized challenge. Open v7
groups drain with their original shared challenge during rollout. Multi-model
workers rotate toward the least recently covered model instead of `models[0]`;
a node already occupying an unfilled group may cover another advertised model
without opening a duplicate group for the blocked model.

Core hard-targets the job internally but sends the worker an ordinary opaque
UUID and a payload with all validator assignment/group/nonce markers removed.
Those fields are restored only into the Core-to-validator evidence response
after completion. Prompt templates and the post-completion zero-den worker ACK
remain retrospective fingerprints. V0 evidence therefore has no economic
authority. A paid audit rail must first be stake/Sybil-gated, budgeted, and
tested against a model-switching worker; simply reporting fake den would corrupt
the payout contract.

The sealed-assignment compatibility mode also withholds target,
model, nonce, policy, and challenge from `GET /v1/validator/assignments`. The
poll response carries an opaque assignment id plus a SHA-256 commitment. After
the worker completes, `POST /v1/validator/probe/{assignment_id}` discloses the
committed fields and the node verifies the seal before signing. Production
enabled the Core flag only after all first-party nodes ran compatible binaries.
This reduces validator/worker pre-collusion
but does not make the public challenge grammar blind, so authority remains none.

## Privacy

Scorecards expose aggregate verdict counts and health only. They must not expose
raw prompts or outputs, expected answers, nonces, signatures, canonical account
IDs, validator identities, private challenge policies, or reference outputs.
Aggregate network health includes completed assignments, vote agreement and
dispute rates, worker/model coverage, and bounded software-version cohorts.
Registered, fresh, and participating counts remain separate from independently
verified operators. Alembic `0026` and the maintainer-only review tool record an
opaque common-control group after off-platform review. Qualification requires at
least 72 hours and 80% of five-minute heartbeat samples; reviews expire after 30
days by default. Every control group counts once and may occupy only one seat in
a probe group. Public health exposes distinct verified and participating group
counts only; group ids and review references remain private.
Stored preview groups continue to use distinct-registration acceptance. The
health API reports reviewed independent quorum separately and explicitly says
it is not yet required for acceptance; neither signal has economic authority.

## Schema And Deployment

Alembic `0020` creates `grid_validators`, adds nullable registration attribution
to existing assignments and attestations, and enforces one attestation per
assignment/validator. Alembic `0021` adds atomic probe attempt counters and
reclaimable leases, preventing concurrent replay from dispatching duplicate
free inference. Alembic `0022` adds shared probe groups, one validator per
canonical account, and DB-enforced one-assignment/one-attestation membership per
validator and group. Alembic `0025` adds the bounded completed-probe result used
for validator crash recovery. Apply all migrations before deploying replay-aware
validator nodes. Existing legacy evidence may remain unbound and must never be
upgraded to authoritative by inference.
Alembic `0026` adds default-unreviewed operator grouping and qualification
state. Deploy it before the independence-aware allocator. It changes no existing
evidence verdict, routing, reward, payout, bond, strike, or slashing behavior.

Finalized assignment and group rows are operational state and are pruned after
90 days by default. Signed attestation rows remain the durable evidence record;
the pruning job does not delete them.

## Next Authority Gate

Before evidence can affect routing or rewards, the network must prove multiple
independently operated nodes in production, complete self-validation controls,
define dispute windows, and make evidence
replayable end to end. Core result replay closes only the Core-to-validator
delivery gap; nodes still need a durable assignment journal and operator-visible
dead-letter recovery. Slashing requires a separate objective-fraud policy and contract
review after those controls are proven.

The accepted post-preview contract and Core-federation sequence is defined in
[`DECENTRALIZATION_ROADMAP.md`](DECENTRALIZATION_ROADMAP.md). It keeps validator
staking and economic authority gated on independent production evidence rather
than registration count.
