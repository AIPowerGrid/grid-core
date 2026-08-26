# Network Decentralization Roadmap

## Status

Accepted architecture, not a live protocol. The production Grid currently has
one coordinator authority. Validator evidence is non-economic, validator
staking is not deployed, and coordinator federation is not live.

The rollout order is deliberate:

1. prove independently operated validator quorum without economic authority;
2. anchor compact validator and reward commitments on Base;
3. introduce trusted-partner Core replicas with one explicit ordering
   authority; and
4. consider multi-writer coordination only after deterministic replay and
   failover are proven.

Hot inference, prompts, outputs, API credentials, and personal identity data
stay off-chain throughout.

## Invariants

1. **No economic authority from node count.** One operator running several
   validators still has one independently controlled identity for quorum
   policy.
2. **Evidence before punishment.** Preview evidence cannot affect routing,
   rewards, strikes, bonds, or slashing.
3. **Objective fraud only.** Subjective quality, slow output, and reference
   disagreement may reduce reputation or make a result inconclusive; they are
   not slashable facts.
4. **One ordered economic history.** Credits, reservations, completion ledger
   entries, revenue, and payouts must have deterministic idempotency keys and a
   single accepted order before more than one Core may write them.
5. **Chain anchoring is asynchronous.** No user inference request waits for a
   Base transaction or RPC call.
6. **Private data stays private.** Base receives identities, bonds, compact
   commitments, reward entitlements, and dispute outcomes, never raw challenge
   or customer content.
7. **Replication is not consensus.** PostgreSQL streaming replication, backups,
   and Redis replicas improve recovery but do not authorize an independent Core
   to accept writes.

## Phase A - Independent Validator Preview

This is the current target.

- 5-10 operators run registered, wallet-signed validator nodes.
- Shared probe groups target five distinct registrations.
- Acceptance requires three matching votes from independently reviewed
  operators.
- Core exposes registered, fresh, participating, and independently verified
  counts separately.
- Evidence remains `economic_effect: none`.

Exit evidence:

- at least five independently controlled operators remain healthy for 30 days;
- assignment delivery, expiry, replay, retry, and durable outbox behavior are
  measured in production;
- agreement and disputed rates are explainable by modality and policy version;
- self-validation and shared-control exclusions are enforced; and
- a replay tool can recompute every finalized preview group from retained
  commitments and bounded evidence.

Registration alone does not satisfy this phase. Operator independence must be
reviewed and recorded through a privacy-safe process. The first off-chain
registry assigns opaque common-control groups, requires a 72-hour sampled
qualification, expires reviews, prevents a group from occupying multiple seats,
and publishes only aggregate counts. It is a conservative precursor to, not a
replacement for, the audited Base registry in Phase B.

## Phase B - Base Validator And Evidence Contracts

Do not deploy these contracts until Phase A exit evidence exists and the
contract suite has an independent audit.

### ValidatorRegistry

Stores compact validator economic identity:

- validator signing wallet;
- operator commitment or approved independence class;
- active, suspended, exiting, or revoked state;
- bond amount and unlock time;
- software/protocol capability commitment; and
- registration and key-rotation events.

It does not store IP addresses, hostnames, API keys, account IDs, prompts,
outputs, or private operator review notes. Core reads finalized registry events
through a background sync and a durable cache.

### EvidenceEpochs

One record per finalized evidence epoch:

- epoch id and time/block bounds;
- Merkle root over canonical accepted attestation records;
- scoring-policy/configuration hash;
- accepted, disputed, and excluded counts;
- publisher set or quorum signature commitment;
- evidence-manifest content hash and retrieval reference; and
- challenge deadline and finalization state.

The off-chain manifest contains only the minimum replayable commitments and
redacted metadata. Raw prompts and outputs remain in bounded private evidence
storage and are disclosed only through a defined dispute process.

### ValidatorRewards

Rewards use a cumulative Merkle distributor or equivalent pull model:

- rewards accrue only for accepted, useful attestations;
- heartbeat, registration, and raw attestation volume earn nothing;
- self-validation, duplicate operator control, late evidence, and inconclusive
  groups are excluded;
- workers and validators claim from funded contracts themselves; and
- publishing a newer cumulative root cannot erase an already claimable amount.

### Disputes And Slashing

A bounded challenge window precedes finalization. A challenger posts a bond and
identifies one objectively checkable fault. Initial slashable classes should be
limited to facts such as:

- signing conflicting verdicts for the same validator and probe group;
- forging or using a signature that does not recover to the registered wallet;
- publishing a Merkle leaf that is absent from the committed evidence root;
- claiming another validator's accepted attestation; or
- provably violating a deterministic, versioned attestation encoding rule.

Bad artistic taste, model disagreement, latency, a failed usefulness task, or a
reference-pool disagreement are not objective-fraud slashing conditions.
Contract-controlled slashes require the dispute result and evidence commitment;
an operator should not have an unrestricted `slash(address)` shortcut.

### Administration

Upgrade, pause, root-publisher, treasury, and dispute roles are separate. Admin
and funding authority live in reviewed Safe multisigs or hardware-controlled
accounts. Reporter services hold only bounded publishing authority and gas.
Every role change and emergency pause is visible on-chain.

## Phase C - Trusted-Partner Core Federation

The first federation is not a permissionless multi-writer database. It is a
small set of identified partner Cores that can verify, replay, serve reads, and
take over through a controlled process.

### C0: Replay Observer

A partner receives signed, append-only Core events and independently rebuilds
the deterministic public/operational state it is authorized to hold. It serves
health and audit views but accepts no user or worker writes.

Exit evidence:

- continuous event sequence and hash-chain verification;
- byte-identical reducer state roots at epoch boundaries;
- documented data minimization and retention policy; and
- restore-from-genesis plus restore-from-snapshot tests.

### C1: Read And Ingress Partner

A partner may terminate authenticated API connections and forward canonical
commands to the active ordering Core. It may serve safe reads from verified
local state. It cannot settle credits, complete jobs, or publish epochs by
itself.

Every forwarded command carries the original account/service authority,
idempotency key, ingress Core identity, expiry, and signature. The active Core
re-verifies authorization rather than trusting a partner-supplied account ID.

### C2: Controlled Failover

One Core holds a time-bounded write lease. Failover requires an explicit
operator/Safe-approved lease transition or a later reviewed quorum protocol.
The successor proves it has replayed through the last committed sequence before
accepting writes. A fenced former leader cannot settle jobs or credits after its
lease expires.

No automatic split-brain merge exists. Conflicting economic histories stop the
network and enter review; they are never reconciled by timestamp or
last-write-wins.

### C3: Multi-Authority Ordering

Only consider this after C2 survives chaos testing. A BFT log or chain-backed
ordering layer may replace the single write lease, but the state machine and
event contract remain the same. Adding more databases or load balancers is not
this phase.

## Signed Core Event Contract

Every replicated state transition uses one canonical envelope:

```json
{
  "schema": "aipg.core.event.v1",
  "origin_core_id": "registered-core-id",
  "sequence": 12345,
  "previous_event_hash": "sha256-hex",
  "event_id": "uuid",
  "event_type": "credit.reserved",
  "aggregate_id": "account-or-job-id",
  "idempotency_key": "bounded-stable-ref",
  "policy_version": "versioned-reducer-policy",
  "payload_hash": "sha256-hex",
  "created_at": "UTC timestamp",
  "signing_key_id": "registered-key-id",
  "signature": "canonical-envelope-signature"
}
```

The private payload is transferred separately only to partners authorized for
that data class. The envelope commits it without placing sensitive content in a
public event stream.

Required verifier behavior:

- reject unknown or revoked Core identities and signing keys;
- reject sequence gaps, duplicate event ids with different bytes, broken
  previous hashes, expired commands, and unsupported policy versions;
- treat an exact duplicate idempotency key and payload as a no-op;
- quarantine conflicting duplicates rather than choosing one;
- apply events through deterministic, side-effect-free reducers; and
- execute external side effects through a durable outbox after state commit.

Economic reducers use integer units only. Floating point may remain telemetry,
never replicated credit, token, or reward truth.

## State Ownership

| State | Replication rule | Base role |
|---|---|---|
| API/service credentials | local encrypted authority; proof events only | none |
| Canonical account links | minimum signed identity events to authorized Cores | identity commitment only if needed |
| Jobs and queue leases | active ordering Core; bounded failover state | none |
| Credit/reservation ledger | fully ordered, append-only economic events | periodic compact audit root |
| Completion/reward ledger | fully ordered, append-only economic events | epoch root and reward root |
| Validator evidence | signed commitments plus retained dispute material | evidence epoch root |
| Worker/validator bonds | finalized event cache | source of truth |
| Prompts and outputs | no general federation; explicit encrypted retention | never |

## Go-Live Gates

Before C1:

- event schema and reducer test vectors are public and versioned;
- mTLS plus application signatures authenticate partner Cores;
- credential, PII, prompt, and output redaction tests pass;
- a partner can rebuild and match epoch state roots; and
- lag, sequence gaps, invalid signatures, and quarantine counts are public
  operational metrics.

Before C2:

- lease fencing is enforced on every economic write and external sender;
- failover is tested during queued, dispatched, streaming, settled, and payout
  states;
- duplicate completion, reservation, deposit, and payout races remain
  idempotent under real PostgreSQL concurrency;
- old-leader recovery cannot resume writes automatically; and
- rollback restores the previous single-Core deployment without rewriting an
  append-only ledger.

Before validator rewards or slashing:

- Phase A exit evidence exists;
- Base contracts and deployment scripts are independently audited;
- cumulative reward and dispute vectors match on-chain and off-chain code;
- monitoring proves funded reward liabilities and claimability; and
- emergency pause cannot confiscate already finalized claims.

## Public Status

The public network status must keep maturity visible:

- online workers and per-model redundancy;
- registered, fresh, participating, and independently verified validators;
- accepted/disputed/finalized group counts and software cohorts;
- charging mode and public payout totals;
- current incidents separately from decentralization advisories;
- `coordinator_federated`; and
- validator economic effect and staking requirement.

Marketing may call the generation supply distributed today. It must not call
validation decentralized until Phase A is proven, or Core federated until at
least C1 is independently operated and continuously replaying signed events.
