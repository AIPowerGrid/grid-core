# grid_api/services/p2p - decentralized dispatch prototype

## Purpose

Default-off libp2p/gossipsub prototype for decentralized job broadcast, worker
discovery, claim coordination, and result streaming.

## Ownership

- `config.py` - P2P env config.
- `node.py` - libp2p host wrapper, gossipsub, DHT/relay plumbing, stream bridge.
- `protocol.py` - `JobRequest`, `JobClaim`, `JobResult` message shapes.
- `topics.py` - topic naming.
- `job_queue.py` - P2P queue interface.
- `hybrid_queue.py` - Redis plus P2P bridge experiment.
- `README.md` - prototype usage notes.

## Local Contracts

- This module is not the production queue. `grid_api.services.job_queue` remains
  the live queue unless a dedicated decentralization rollout changes that.
- Do not enable P2P for money-moving or payout-bearing traffic until persistent
  node identity, signed job claims, worker signer registry, replay protection,
  peer scoring, and validator review are implemented.
- `P2P_PRIVATE_KEY_PATH` must actually load a stable key before partner nodes
  rely on peer identity.
- P2P messages are untrusted network input. Validate size, TTL, signature, model,
  and requester identity before executing or relaying.
- Use Base as the root for trusted partner node/operator registry and slashing,
  not gossipsub self-declaration.

## Work Guidance

- Keep this code isolated from live dispatch until tests cover multi-node claim
  conflicts, duplicate results, malicious claims, stale jobs, and reconnects.
- Prefer signed canonical message structs over ad-hoc JSON fields for anything
  that affects billing, ledger, reputation, or settlement.
- If P2P becomes active, update parent docs, deploy docs, env template, and the
  worker onboarding docs in the same change.

## Verification

- No complete automated P2P verification exists yet.
- Add deterministic protocol/unit tests before changing claim or result rules.

## Child DOX Index

- None - leaf.
