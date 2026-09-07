# Validator Compensation Pilot

Status: **allocation and recipient-consent backend, not deployed or payment-enabled**.
The manual PostgreSQL path exists alongside the earlier offline simulation.
No real campaign/budget has been approved or created, funds are not reserved
on-chain, and no payout adapter is implemented or enabled.
This is separate from worker den and from paying workers to execute blind audits.
It cannot activate routing, reputation penalties, bonds, or slashing.

## Proposed Terms

- At most 10,000 AIPG total over at most seven days.
- At most 2,000 AIPG per reviewed independent operator across all their nodes.
- At most 100 reviewed contributions per operator per UTC day.
- One unit per operator and probe group, not per node, retry or heartbeat.
- Exclude first-party, unreviewed, rejected and expired-review operators.
  Objectively incorrect task scores do not count. Peer-quorum disagreement
  alone does not invalidate a correctly scored task or reward majority voting.
- Allocate pro rata by reviewed units, floor to integer base units, then apply
  the operator cap. Leave both rounding and capped remainders in treasury;
  do not redistribute them or automatically raise any budget.

These are maximum draft terms, not payment promises. The maintainer must approve
the budget, earning window, eligibility and beneficiary accounts before the pilot.
Resolve separately proven payout destinations before any transfer.
Publish the terms before earning starts. Do not retrofit this draft onto past
unpaid participation without a separate explicit decision.

## Durable Allocation Contract

Migration `0036` creates three empty private tables: campaign contracts,
allocations and work claims. No automatic runtime handler or worker payout
timer imports this service. The administrative command requires PostgreSQL;
SQLite remains migration-compatible, not a qualified monetary runtime.

`scripts/manage_validator_compensation.py create` reads a private object with
exactly `terms` and `validator_ids`. Terms contain the six fields in the offline
terms table below plus `software_version` and an opaque `approval:<reference>`.
There are no default amounts. A reference records an actual owner decision; it
is not authentication or proof of consent by itself. Only an authorized
maintainer with the database role may apply it after obtaining that decision.

The durable pilot is exactly seven days, frozen before its start and at most
one day ahead. It selects three to ten active, reviewed independent operators,
one node/beneficiary per control group for this first pilot. All must advertise
the exact admitted release and have a fresh heartbeat at creation. Existing
independence reviews must cover the full window plus one-hour receipt grace.
The campaign commits their accounts, signers, control groups, review records,
version, budget, caps, Base chain, configured AIPG token and 18-decimal unit.
Independent control and first-party exclusion still depend on the maintainer's
review, not a node's self-description or a hash. No qualification is reset.

Finalization runs after the fixed end plus one hour. It reads at most 10,000
authoritative receipts from the selected members, re-verifies their signatures
and exact account/signer/assignment/nonce/evidence/target bindings, and requires
a completed `text.generated.v8` task. Assignments must originate and complete
inside the pilot; receipts must arrive inside the fixed grace and their own
assignment deadline. Missing or corrupt binding data blocks finalization.
Other experimental policies are excluded, never silently paid as text work.

The signed verdict must match Core's objective task result. This accepts a
correct `failed` verdict as well as `healthy` or `slow`; peer quorum outcome is
not an input. It proves neither model identity nor independence from a dishonest
coordinator. Frozen membership/review drift requires review before finalization;
an operator simply going offline after completing work does not erase it.

Eligible work is capped per UTC completion day, selected in server-receipt then
receipt-ID order. Each control group/probe group and assignment can be allocated
once globally, including across renamed/overlapping campaigns. Finalization
rechecks eligibility under transaction locks, recomputes the reviewed digest,
and atomically inserts integer allocations and work claims with the campaign
terminal. A conflict or failure rolls everything back. Capped and rounding
remainders stay unallocated. Re-running a finalized campaign reconciles its
stored commitments, beneficiaries, amounts and work counts before returning it.
Retained verification facts and signed reports support audit after assignment
pruning. This is tamper-evident accounting, not protection against a malicious
database administrator who can rewrite every commitment.

An allocation belongs to a frozen account. It has no automatic recipient,
nonce or transaction; the node's generated signing wallet is not a fallback
payout destination. `sendable` remains false. Allocation finalization is not
a passing seven-day operational pilot or authorization to turn on penalties.

Administrative sequence, using private paths chosen by the maintainer:

```sh
.venv/bin/python scripts/manage_validator_compensation.py create \
  --input /private/pilot-request.json --output /private/pilot-preview.json
.venv/bin/python scripts/manage_validator_compensation.py create \
  --input /private/pilot-request.json --output /private/pilot-approved.json \
  --apply --expect-digest <reviewed-preview-digest>
.venv/bin/python scripts/manage_validator_compensation.py finalize \
  --campaign-id <approved-campaign-id> --output /private/allocation-preview.json
.venv/bin/python scripts/manage_validator_compensation.py finalize \
  --campaign-id <approved-campaign-id> --output /private/allocation-approved.json \
  --apply --expect-digest <reviewed-allocation-digest>
```

These are placeholders, not approved production commands. Preview connections
are read-only and never call schema initialization. Full results stay in
exclusive `0600` outputs; stdout contains aggregates and commitments only. A
failed output write can follow a committed transaction: retry the same campaign
and digest with a new private output path. Never create a replacement campaign
to recover an uncertain result. The complete create preview/apply path and
finalization have been exercised against synthetic PostgreSQL records, not an
approved live compensation cohort. Operator consent UI, transfer execution,
transaction reconciliation and compensation status UI are still required.

## Recipient Consent Backend

Migration `0037` adds one private immutable consent row per finalized positive
allocation. This administrative backend exists; a public endpoint and local-app
wallet flow do not yet exist. Do not instruct operators to paste private keys
or run the administrative command. Node signing must eventually use the existing
local node key internally; the human approves in their chosen payout wallet.

`scripts/manage_validator_recipient.py prepare` reads exactly `campaign_id`,
`operator_group_id` and a canonical lowercase `recipient` from a protected JSON
file. Its private output contains `consent` plus the exact EIP-191 `message`.
It does not create a challenge or payment in the database. The signing window
is at most 24 hours. The consent commits a versioned domain and Grid audience,
campaign/contract/allocation hashes, frozen account/node/signer/control group,
Base chain 8453, token, exact integer amount, recipient and timestamps.

The node and payout recipient sign the same text. A bind input contains exactly
`consent`, `node_signature`, `recipient_signature`, and an opaque
`approval:<reference>`. No key is accepted. EOA signatures verify offline;
deployed contract recipients require a Base chain check and the existing
fail-closed EIP-1271 verifier. Wrong-chain/unavailable RPC, invalid signatures,
expired consent and identity/commitment drift are rejected. The target cannot
be zero, the token contract or the funds-less node signer. There is no fallback
to the account login wallet, worker payout preference or paired human account.

The reviewer must independently confirm the destination with the known operator.
Two signatures prevent an unsigned destination substitution; they do not prove
that a compromised node key plus an attacker-controlled destination belongs to
the legitimate operator. A review reference records the maintainer's actual
decision, not automatic authorization or a proof of independence.

Bind defaults to a read-only preview. Apply requires the exact preview digest,
serializes with allocation mutations under PostgreSQL, locks the current node
and account, and rechecks expiry after signature verification. Retired accounts
and changed node signers require manual recovery, not alias-following. Going
offline or an independence review expiring after finalized work does not erase
earned allocation ownership. A matching committed request can be retried after
its signing deadline; a changed destination/proof cannot overwrite it. Recipient
correction or identity recovery requires a separately reviewed workflow, which
must also check that no payment is in flight; it is not implemented by this CLI.

Maintainer-only sequence, using private paths and existing finalized test data:

```sh
.venv/bin/python scripts/manage_validator_recipient.py prepare \
  --input /private/recipient-request.json --output /private/consent-to-sign.json
# Have both clients sign the exact message outside Core; review the destination.
.venv/bin/python scripts/manage_validator_recipient.py bind \
  --input /private/signed-consent.json --output /private/recipient-preview.json
.venv/bin/python scripts/manage_validator_recipient.py bind \
  --input /private/signed-consent.json --output /private/recipient-approved.json \
  --apply --expect-digest <reviewed-recipient-digest>
```

All paths are placeholders. New outputs are owned `0600` files, never overwritten;
stdout omits identities, addresses, signatures and the signing message. After an
uncertain result retry the same signed input/digest with a new private output
path. `sendable` remains false even after a successful bind. No nonce, payout
attempt, wallet transaction or budget activation occurs. The sender must still
screen recipients and prove confirmed transfers; consent alone is not payment.

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
work. Unreproducible scoring is not reviewed work; peer disagreement by itself
does not disqualify an independently verified objective score.

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
   and verdict review. Resolve a separately proven recipient wallet bound to
   the frozen beneficiary account; never assume an ephemeral validator signer is the
   desired payout destination.
2. Deploy and qualify the implemented campaign/allocation/work ledger, then
   qualify the recipient backend and ship the operator consent flow. PostgreSQL allocation
   deduplication alone does not prevent a sender paying twice.
3. Bind approved allocations to payment attempts without changing their frozen
   caps, amounts or evidence. Freeze the recipient before broadcast. An expired
   or changed review/recipient invalidates a draft, not a broadcast payment.
4. Build a campaign adapter sharing the existing treasury nonce lock. Do not
   invent worker den or feed this simulation into the ordinary worker CLI.
5. Test concurrent duplication, renamed campaigns, partial batches, pending
   receipts, retries and crashes against PostgreSQL and the verified-transfer
   path. Require matching token Transfer evidence, not merely receipt success.
6. Approve an exact capped manifest and supervised small transfer separately.
   Check receipts and replay idempotency before enabling any recurring payout.

No gate above is satisfied merely because the allocation unit tests pass.
