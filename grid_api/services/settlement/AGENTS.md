# grid_api/services/settlement - worker payout rails (live custodial + pass-through + future trustless)

> Three rails live here. (1) **LIVE:** custodial `payouts.py` — hourly systemd
> timer, fixed AIPG budget, treasury hot wallet. (2) **BUILT, DARK:** the
> pass-through multi-asset rail (`revenue.py` + `multiasset.py`) — the DECIDED
> payout model, gated on funded treasury + live charging. (3) **FUTURE, STUB:**
> the trustless on-chain claim rail (`bot.py` Merkle → reportPeriod → claim) —
> Diamond facets not deployed. Never present (3) as running.

## Purpose

Pay workers for metered grid usage. Today: custodial AIPG pro-rata by den.
Next: pro-rata **pass-through** — distribute the actual revenue basket
(USDC/ETH/AIPG) per asset by den, **no conversion** (same basket in, same basket
out; that property is what keeps the grid out of money-transmitter/exchange
territory — see `docs/architecture/PAYOUT_EXECUTOR.md`). Eventually: trustless
Merkle claims on Base.

## Ownership

**Live custodial rail:**
- `payouts.py` - custodial CLI/timer: fixed AIPG budget pro-rata by den,
  nonce-bound, Transfer-proven, idempotent per (period, account). **Every send
  is OFAC-gated** (`sanctions.screen` before funds move). Its fresh-nonce
  allocator spans BOTH rails (`grid_payouts` AND `grid_payout_legs` — one
  treasury account = one nonce space).
- `sanctions.py` - OFAC screening: local denylist (`GRID_SANCTIONS_DENYLIST`,
  authoritative, zero-I/O) + optional Chainalysis oracle
  (`GRID_SANCTIONS_ORACLE`). FAIL-CLOSED: hit → `blocked_sanctions`;
  oracle-configured-but-unreachable → `review_sanctions` (hold, never pay blind).

**Pass-through multi-asset rail (dark):**
- `revenue.py` - per-asset revenue pots (`grid_revenue`, append-only, idempotent
  on ref) + `compute_multiasset_payouts` (PURE: den-share of EACH asset pot,
  conserves every pot) + `worker_pots` (85% worker share per asset via
  `economics.worker_share_bps`). ⚠️ The intake feed (`record_revenue` callers)
  must be EARNED revenue (consumption, deposit-lineage), never raw deposits.
- `multiasset.py` - the per-leg Base sender (`send_period_multiasset`): inherits
  every payouts.py invariant (OFAC gate, nonce-bind-before-broadcast,
  consumed-nonce-unproven → manual_review, escalating replacement fees).
  Proof per kind: ERC-20 = matching Transfer log from THAT token contract;
  **native ETH = the tx itself (to+value) + status-1 receipt** (no Transfer log
  exists for native value). Legs in `grid_payout_legs`, idempotent per
  (period, account, asset). Unsupported pot assets are held loudly, never guessed.
- `assets.py` - payout asset registry (AIPG/USDC erc20 + Base addresses +
  decimals; ETH native; `to_base_units`).

**Future trustless rail (stub):**
- `bot.py` - the settlement bot (orchestrates a settlement run).
- `merkle.py` - cumulative Merkle tree + proof generation.
- `aggregate.py` - roll up per-worker/per-den earnings for a period (input to ALL rails).
- `ipfs.py` - publish the proof set off-chain.
- `tests/` - `test_merkle.py`, `test_ipfs.py`.

## Local Contracts

- **Money moves only after OFAC screening.** A screen hit or an unverifiable
  address is a terminal row status, never a send.
- **Proof, not trust:** a payout is `sent` ONLY on a proven receipt (matching
  Transfer log, or tx to+value for native). status==1 alone is NOT proof. A
  consumed nonce that can't be proven becomes `manual_review` — never re-sent,
  never auto-`sent`.
- **One nonce space:** fresh nonces exceed the max bound in `grid_payouts` AND
  `grid_payout_legs`; both rails share the treasury account.
- **No conversion in any rail.** The pass-through model distributes the basket
  as received; swaps/fees were deliberately rejected (exchange/MSB exposure).
  Fiat/USDC off-chain legs are the Stripe rail (design: PAYOUT_EXECUTOR.md).
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

- `pytest grid_api/services/settlement/tests/` (Merkle/IPFS).
- `pytest grid_api/services/tests/test_revenue.py test_multiasset.py test_sanctions.py`
  (pass-through engine, sender routing + proofs, OFAC gate).
- Add bot integration tests before enabling live settlement.

## Child DOX Index

- None - leaf.
