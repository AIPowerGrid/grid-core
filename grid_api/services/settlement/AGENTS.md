# grid_api/services/settlement - on-chain reward settlement (FUTURE rail)

> ⚠️ **NOT the live payout system.** This directory is the future trustless
> on-chain claim rail (Merkle → reportPeriod → worker claim), and `bot.py` is a
> stub — the Diamond facets are not deployed. Workers are paid TODAY by the
> **custodial CLI `payouts.py`** (hourly systemd timer, treasury hot wallet) —
> see `GO_LIVE.md`. Never present this settlement code as the running payout rail.

## Purpose

Turn metered grid usage into on-chain reward distributions: build cumulative Merkle roots
of earnings, publish proofs (IPFS), and submit settlement on Base. This is the *planned*
trustless replacement for the live custodial `payouts.py` rail, not a running system.

## Ownership

- `bot.py` - the settlement bot (orchestrates a settlement run).
- `merkle.py` - cumulative Merkle tree + proof generation.
- `aggregate.py` - roll up per-worker/per-den earnings for a period.
- `ipfs.py` - publish the proof set off-chain.
- `tests/` - `test_merkle.py`, `test_ipfs.py`.

## Local Contracts

- **Current posture:** `bot.py` is a stub. Do not present settlement as live
  until DB aggregation, Safe/multisig reporting, Base RPC reads, claim batching,
  dry-run/once CLI, and durable state are implemented and tested.
- Settlement input is `grid_ledger` via `aggregate.py`; do not read orphan or
  legacy den tables for v2 worker payouts.
- Merkle leaf and proof formats are wire contracts with on-chain claim logic.
  Any format change must update tests and known vectors.
- A settlement run must be idempotent: repeated runs must not double-report,
  double-claim, skip closed periods, or pay wallets without ledger support.
- IPFS pinning can fail without aborting a settlement only if the on-chain root
  and local durable proof artifact remain retrievable by ops.
- Reporter/hot wallets are gas-only. Admin/funding wallets must remain hardware
  or multisig controlled and outside process env.

## Work Guidance

- Any change to root construction requires updating `test_merkle.py` and re-deriving a known
  vector. Treat the Merkle format as a wire contract with on-chain claim logic.
- Keep docs honest: if `GO_LIVE.md` describes a command, `bot.py` must actually
  implement that CLI and env names must match.
- Prefer small pure functions for period boundaries, aggregation, snapshot
  serialization, proof generation, and transaction planning so dry-runs can be
  tested without Base RPC.

## Verification

- `pytest grid_api/services/settlement/tests/`.
- Add bot integration tests before enabling live settlement.

## Child DOX Index

- None - leaf.
