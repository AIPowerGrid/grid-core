# grid_api/services/settlement — on-chain reward settlement

## Purpose

Turn metered grid usage into on-chain reward distributions: build cumulative Merkle roots
of earnings, publish proofs (IPFS), and submit settlement on Base.

## Ownership

- `bot.py` — the settlement bot (orchestrates a settlement run).
- `merkle.py` — cumulative Merkle tree + proof generation.
- `aggregate.py` — roll up per-worker/per-den earnings for a period.
- `ipfs.py` — publish the proof set off-chain.
- `tests/` — `test_merkle.py`, `test_ipfs.py`.

## Local Contracts

- **Cumulative roots:** each root supersedes the prior; a claim proves `total_earned` to date
  minus already-claimed. Never publish a non-cumulative or decreasing root.
- **Idempotency:** a settlement run must be safe to re-run; the ledger dedupes (no double-pay).
- **Pre-money gate:** real settlement is blocked until contracts pre-cut hardening (#46) and
  the open economic gaps (wallet sig, model-name multiplier, den scaling, token count) close.

## Work Guidance

- Any change to root construction requires updating `test_merkle.py` and re-deriving a known
  vector. Treat the Merkle format as a wire contract with on-chain claim logic.

## Verification

- `pytest grid_api/services/settlement/tests/`.

## Child DOX Index

- None — leaf.
