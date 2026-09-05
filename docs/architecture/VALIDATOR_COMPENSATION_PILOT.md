# Validator Compensation Pilot

Status: **proposal and offline allocation simulation only**. No campaign is
earning, no funds are reserved, and no payout adapter is implemented or enabled.
This is separate from worker den and from paying workers to execute blind audits.
It cannot activate routing, reputation penalties, bonds, or slashing.

## Proposed Terms

- At most 10,000 AIPG total over at most seven days.
- At most 2,000 AIPG per reviewed independent operator across all their nodes.
- At most 100 reviewed contributions per operator per UTC day.
- One unit per operator and probe group, not per node, retry or heartbeat.
- Exclude first-party, unreviewed, rejected, expired-review and disputed work.
- Allocate pro rata by reviewed units, floor to integer base units, then apply
  the operator cap. Leave both rounding and capped remainders in treasury;
  do not redistribute them or automatically raise any budget.

These are maximum draft terms, not payment promises. The maintainer must approve
the budget, earning window, eligibility and exact recipients before the pilot.
Publish the terms before earning starts. Do not retrofit this draft onto past
unpaid participation without a separate explicit decision.

## Offline Tool

`scripts/preview_validator_compensation.py` consumes a private reviewer snapshot
and writes a private, **non-sendable** allocation simulation. It has no network,
database, wallet, signing, or settlement imports and no `--apply`/`--send` mode.
It deliberately does not produce recipient addresses or executable transfers.

The input is an assertion by its preparer, not verified evidence. In particular,
`review_status: verified` and a digest do not establish operator independence,
valid signatures, accepted work, or correct verdicts. An independent Core-backed
review/import stage is still required before any real entitlement can exist.

Input has exactly three fields:

| Field | Content |
|---|---|
| `terms` | `campaign_id`, timezone-aware `starts_at`/`ends_at`, decimal strings `budget_atomic`/`operator_cap_atomic`, integer `daily_unit_cap` |
| `operators` | Up to 100 private operator-review records |
| `contributions` | Up to 10,000 already-reviewed contribution records |

Each operator record has `operator_group_id` (opaque `opg_*`), `first_party`
(boolean), `review_status` (`verified`, `unreviewed`, `rejected`), timezone-aware
`reviewed_at`/`expires_at`, and a lowercase SHA-256 `review_digest` committing
the private review. Include every node under common control in one operator.
This tool cannot detect an incorrect or malicious common-control classification.

Each contribution has `assignment_id`, `operator_group_id`, `probe_group_id`,
timezone-aware `completed_at`, and lowercase SHA-256 `evidence_digest`. Only
include evidence independently checked against Core's assignment, signer,
nonce, commitment, timely acceptance and reviewed verdict. A healthy verdict
or quorum agreement alone is not sufficient. Failed-worker evidence can be valid
work; disputed/unreproducible evidence must remain outside the reviewed input.

The tool normalizes times to UTC. Exact assignment replays are idempotent;
conflicting versions of the same assignment fail. Different assignments for the
same operator/group count once. Earliest completion then assignment ID chooses
the daily-cap order, independently of input order. Marking a group counted before
the daily cap prevents a duplicate from moving to a later day to evade the cap.
Work must fall in `[starts_at, ends_at)` and not be in the future relative to
`--as-of`. The operator review must cover both completion and preview time.

Amounts use decimal strings to avoid JavaScript's integer precision limit.
The draft uses 18 base-unit decimals per AIPG; the eventual sender must verify
the real Base token/chain/decimals rather than inherit this planning assumption.

Run from a reviewed Core checkout with a POSIX account that owns the private
input file (mode `0600`). Supply an unused output path on protected storage:

```sh
.venv/bin/python scripts/preview_validator_compensation.py \
  --input /private/reviewed-validator-work.json \
  --output /private/validator-pilot-draft.json \
  --as-of 2026-09-08T00:00:00Z
```

The path/time above are placeholders, not production configuration. The command
is tested locally against synthetic private files; it has not processed an
approved real operator/payment snapshot. Input is limited to 2 MiB; duplicate
JSON keys, symbolic-link inputs and non-private input files are rejected.
Output is exclusively created with mode `0600`; an existing file is never
overwritten. A failed write may leave a private incomplete draft; inspect it and
choose a new output path rather than treating it as a successful result.
Stdout contains aggregate totals and a simulation digest, not
private operator groups, paths, review records or contribution identities.

`dry_run: true`, `sendable: false`, and
`input_authority: unverified_reviewer_snapshot` are unconditional. The digest
commits normalized terms, review records, unique contributions and snapshot time.
It is not a signature, evidence validation, budget approval or global replay lock.
Empty/ineligible input allocates zero. Reordering/exact retransmission does not
change the result. A changed campaign name creates another simulation, **not**
a second payable entitlement.

## Gates Before Sending

1. Independently verify operator control, first-party exclusion, accepted work
   and verdict review. Resolve a separately proven recipient wallet through
   the canonical account; never assume an ephemeral validator signer is the
   desired payout destination.
2. Implement immutable campaign, contribution and approved-recipient records
   in PostgreSQL, including cross-campaign reuse protection. Offline
   deduplication does not prevent concurrent senders or repeated payment runs.
3. Enforce campaign/operator caps transactionally and freeze amounts, evidence
   and recipient before broadcast. An expired or changed review/recipient
   invalidates a draft, not an already broadcast payment.
4. Build a campaign adapter sharing the existing treasury nonce lock. Do not
   invent worker den or feed this simulation into the ordinary worker CLI.
5. Test concurrent duplication, renamed campaigns, partial batches, pending
   receipts, retries and crashes against PostgreSQL and the verified-transfer
   path. Require matching token Transfer evidence, not merely receipt success.
6. Approve an exact capped manifest and supervised small transfer separately.
   Check receipts and replay idempotency before enabling any recurring payout.

No gate above is satisfied merely because the allocation unit tests pass.
