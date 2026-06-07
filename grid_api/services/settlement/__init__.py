# SPDX-License-Identifier: AGPL-3.0-or-later

"""On-chain settlement of worker den rewards.

This package replaces the legacy `grid-rewards-sentry` Flask app. Instead of
batch-minting AIPG to a hardcoded set of worker addresses via legacy L1
JSON-RPC, the settlement bot here:

  1. Aggregates worker den for a closed period (typically the prior UTC day).
  2. Pins the full [worker, den] list as JSON to IPFS for off-chain audit.
  3. Builds a Merkle tree of the entries and computes the root.
  4. Submits `DenReporter.reportPeriod(periodId, root, totalDen, ipfsUri)`
     via the team multisig (signs off-chain, multisig executes).
  5. Pushes per-worker payouts in batches via
     `PaymentRouter.claimBatch(periodId, workers, den, proofs)` so workers
     never need to hold Base ETH to receive earnings.

Three contract facets sit on the other end (in aipg-smart-contracts/contracts/grid/modules):

  - RewardPool     — holds AIPG, decoupled from payout rate
  - DenReporter    — receives per-period commit (Merkle root + IPFS URI)
  - PaymentRouter  — workers claim or anyone relays

Modules:

  ipfs.py      — IPFS pinning helper (production-ready)
  merkle.py    — Merkle tree builder matching contract verify convention
  bot.py       — scheduler + multisig signer + claim batcher (stub)
"""
