# Validator Budget Compatibility Deployment

## Production Release

Core `f5211254b8d453fb1f8af025f201221203205b24` replaced `d606e4d8`
at **2026-09-15T13:53:06Z**. PR190 was normally squash-merged after its
required checks passed. The deployed tree exactly matches tested PR head
`f6986a70c6e8c7e329a4f64040b7d760b395f80a`.

The only runtime change relative to the previous release raises the explicit
validator compensation ceilings to 100,000 AIPG total and 25,000 AIPG per
operator. Other changes are tests and documentation. No dependency, schema,
billing or worker-payout implementation changed.

The detached candidate uses Python 3.12 and hash-locked binary dependencies;
`pip check` passed. A fresh production backup from 13:49:05 UTC was restored
into a generated disposable database. Candidate migration and schema checks
passed at Alembic `0042`, with no new upgrade operations. The restore proof
removed its scratch database. No production migration was needed.

## Cutover Verification

Temporary bounded generation/probe route maintenance and stopped MCP ingress
allowed two quiet checks before stopping Core, followed by another quiet check
before the atomic release switch. All checks found zero held reservations,
zero pending stream deliveries and no entries after either consumer group's
last-delivered ID. Redis reported text `lag=9` despite that empty undelivered
range; the direct range and pending checks established actual emptiness. No
queue entry was deleted and no consumer cursor was changed.

Core and MCP restarted, and the temporary maintenance overlay was removed.
Local and public health independently returned the exact new commit, healthy
Redis and all nine previously connected workers. Environment and worker-payout
start-boundary drop-in hashes were unchanged. The payout timer remained enabled
and active. The payout service already had a failed invocation before deployment;
this release did not retry it or establish payout health.

## Campaign Status

The compatibility deployment itself did not activate an earning campaign. The
initial post-cutover database contained zero validator campaigns or payment rows.
No independence review, qualification reset, recipient binding or transfer was
performed. Existing billing, validator admission, penalty, media and shadow
controls were preserved.

The owner-approved pilot remains seven days, at most 100,000 AIPG total,
25,000 AIPG per independent operator and 100 reviewed units per operator per UTC
day. The prior smaller budget is superseded, not additive. Pay only reviewed,
accepted work under a frozen prospective campaign; do not backdate its start.
Unused or capped budget is not automatically redistributed.

A read-only September 15 check found four known public operators fresh on
preview.20. Three meet the existing technical qualification thresholds, with
recent 72-hour heartbeat coverage of 100%, 100% and 96.8%, completed workloads
and authoritative attestations. The fourth is back online but has 19.9%
coverage, below the existing 80% threshold. These are point-in-time technical
observations, not independent-control attestations. Identity details and review
artifacts remain private.

The subsequently approved campaign below supersedes the initial pending-review
posture. Payout-wallet consent is required before transfers, not before earning
starts. Registration signing wallets are not automatically payout destinations.

## Frozen Earning Campaign

The sole maintainer explicitly confirmed independent control of the three
technically eligible public operators. Digest-bound review transitions committed
successfully using the deployed review service. Existing qualification and
heartbeat history were preserved; no new observation period was imposed.

At **2026-09-15T14:11:33Z**, the official compensation command committed
`validator-pilot-20260915` after a read-only preview and exact-digest approval.
A separate read-only database query confirmed one open contract with three
members, three verified operator records and zero validator payment rows.

| Term | Frozen Value |
| --- | --- |
| Earning starts | September 15, 2026, 14:30 UTC |
| Earning ends | September 22, 2026, 14:30 UTC |
| Earliest finalization | September 22, 2026, 15:30 UTC, after receipt grace |
| Software | `v0.1.0-preview.20` |
| Eligible policy | `text.generated.v8` |
| Approved total ceiling | 100,000 AIPG |
| Per-operator ceiling | 25,000 AIPG |
| Daily contribution cap | 100 reviewed units per operator per UTC day |
| Frozen independently controlled members | 3 |

Contract commitment:
`52cdf64449e50652c93241a0182b0c098f47360a70a2ba9a4ffca1b5766d9f36`.

Three members mean this campaign can allocate at most 75,000 AIPG despite its
100,000 AIPG total ceiling. Amounts depend on verified eligible work, are not
guaranteed fixed payments, and unused budget stays unallocated. Membership and
dates are frozen; do not silently append recovering or new nodes, backdate work,
or create an additional overlapping budget. First-party nodes are excluded.

Work evidence continues through the existing assignment/attestation pipeline.
After the earning window and one-hour receipt grace, preview finalization,
independently review accepted contributions, and apply the exact allocation
digest. Obtain allocation-specific recipient consent before any separately
approved transfer. The campaign creation did not enable the sender, invoke the
worker payout timer, send funds, or grant worker penalty authority.

## Omitted Operators: Supplement Deployed

Complete fleet intake identified two further technically qualified public
operators. After owner confirmation, both reviews passed through the deployed
digest-bound service, preserving observation history. One stale candidate preview
was rejected after a heartbeat update; a fresh preview applied successfully.

PR192 passed every required check, including PostgreSQL 16, the full suite,
backup/restore and pinned client integration. Its normally merged commit
`bc49519c6dbd94f46828d190b0ea0300a30b3761` exactly matches reviewed head
`901f94f872006bd3388cd8b0f8ca20aa791caec7`. Production selected it at
**2026-09-15T16:41:03Z** after a fresh 16:39:05 UTC backup, successful scratch
restore/Alembic `0042` drift check, two quiet queue/hold checks and a further
check after Core stopped. No live migration was needed. Environment and payout
drop-in hashes and timer state were preserved. Core/MCP restarted, the temporary
gate was removed, and public health verified the new SHA and all nine workers.

The official command previewed and committed the linked campaign
`validator-pilot-20260915-supplement` for the two separately reviewed members:

- Starts September 15, 2026, **16:45 UTC**.
- Ends September 22, 2026, **14:30 UTC**, matching the parent.
- Shares **25,000 AIPG total**, allocated by verified eligible work; not a
  guaranteed 25,000 per person. The per-operator ceiling remains 25,000.
- Retains preview.20, `text.generated.v8`, 100 daily units and one-hour receipt
  grace, with evidence verification, deduplication and recipient consent.

Supplement commitment:
`1f00b8d965743c11c66624ffb36b6f638ffb07f7532a81b3da4d98dfe56ae5c1`.

Independent read-only verification found five verified operators, two open
contracts and zero payment rows. The original contract hash, members and dates
are unchanged. Its maximum allocation of 75,000 plus the supplement's full
25,000 budget is bounded by the existing approved 100,000 maximum liability.
Do not sum the two contracts' nominal budget fields as new spending authority.

Operators do not register again, replace keys, restart qualification, or install
a special release to participate. Their existing preview.20 node IDs are the
frozen members; they keep those nodes running. This verifies enrollment and
prospective earning windows, not completed-work allocations or transfers.
