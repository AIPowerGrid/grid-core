# AIPG P2P Module

Decentralized job distribution using libp2p gossipsub.

## Overview

This module provides P2P networking for the Grid API, enabling:
- Decentralized job broadcast (no central Redis required)
- Worker discovery via DHT
- Claim coordination to prevent double-processing

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Gateway (HTTP API)                                     │
│  ├── Receives requests from users                       │
│  ├── Publishes jobs to gossipsub                        │
│  └── Subscribes to results                              │
└─────────────────────────┬───────────────────────────────┘
                          │ gossipsub
                          ▼
┌─────────────────────────────────────────────────────────┐
│  P2P Mesh (libp2p)                                      │
│  Topics:                                                │
│    /aipg/1/jobs/{model}  - Job broadcasts               │
│    /aipg/1/claims        - Claim announcements          │
│    /aipg/1/results/{id}  - Result streaming             │
└─────────────────────────┬───────────────────────────────┘
                          │ gossipsub
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Workers (P2P nodes)                                    │
│  ├── Subscribe to model topics                          │
│  ├── Claim jobs deterministically                       │
│  └── Stream results back                                │
└─────────────────────────────────────────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Package exports |
| `config.py` | P2P configuration from environment |
| `protocol.py` | Message types (JobRequest, JobClaim, JobResult) |
| `topics.py` | Gossipsub topic management |
| `node.py` | libp2p node wrapper |
| `job_queue.py` | P2P job queue (same interface as Redis version) |
| `hybrid_queue.py` | Redis + P2P hybrid queue |

## Configuration

Set these environment variables:

```bash
# Enable P2P mode
P2P_ENABLED=true

# Network identity (optional - generates new key if not set)
P2P_PRIVATE_KEY_PATH=/path/to/private.key

# Listen address
P2P_LISTEN_HOST=0.0.0.0
P2P_LISTEN_PORT=4001

# Bootstrap peers (comma-separated multiaddrs)
P2P_BOOTSTRAP_PEERS=/ip4/1.2.3.4/tcp/4001/p2p/QmPeer1,/ip4/5.6.7.8/tcp/4001/p2p/QmPeer2

# Gossipsub tuning
P2P_GOSSIP_DEGREE=6
P2P_GOSSIP_DEGREE_LOW=4
P2P_GOSSIP_DEGREE_HIGH=12

# Topic prefix
P2P_TOPIC_PREFIX=/aipg/1

# Timeouts
P2P_CLAIM_TIMEOUT=5.0
P2P_JOB_TTL=60
```

## Usage

### Gateway (submit jobs)

```python
from grid_api.services.p2p.hybrid_queue import submit_job

# Submit a job (goes to P2P if enabled, always to Redis)
await submit_job(job_id, payload, models=["llama3.2:3b"])
```

### Worker (receive jobs)

```python
from grid_api.services.p2p import get_p2p_node
from grid_api.services.p2p.job_queue import register_worker, pop_job, claim_job

# Register for models
await register_worker(["llama3.2:3b", "mistral:7b"])

# Process jobs
while True:
    job = await pop_job(worker_id)
    if job:
        if await claim_job(job["job_id"], worker_id):
            # Process the job
            pass
```

## Claim Resolution

Multiple workers may see the same job. We use deterministic hash-based selection:

```python
def should_claim(job, my_worker_id, known_workers):
    seed = job.signature[:32]  # Random seed from job
    my_score = sha256(job_id + seed + my_worker_id)

    for worker in known_workers:
        if sha256(job_id + seed + worker) < my_score:
            return False  # Someone else should claim

    return True
```

All workers compute the same result independently - no coordination needed.

## Dependencies

```
pip install libp2p trio
```

## Testing

```bash
# Start node 1 (gateway)
P2P_ENABLED=true P2P_LISTEN_PORT=4001 python -m grid_api.main

# Start node 2 (worker)
P2P_ENABLED=true P2P_LISTEN_PORT=4002 \
  P2P_BOOTSTRAP_PEERS=/ip4/127.0.0.1/tcp/4001/p2p/<peer_id_from_node_1> \
  python -m your_worker
```

## Future Work

- [ ] Payment channel integration
- [ ] On-chain worker registry bootstrap
- [ ] Relay node support for NAT traversal
- [ ] Persistent peer ID management
- [ ] Peer scoring and reputation
