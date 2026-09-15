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

This is a compatibility deployment, **not an active earning campaign**. The
post-cutover database still contains zero validator campaigns or payment rows.
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

Next: obtain factual operator-control confirmation, preview/apply each eligible
operator review while preserving qualification history, then preview/apply a
fixed future seven-day campaign and publish its actual dates. Three eligible
independent operators suffice; a fourth operator's recovery need not delay them.
Payout-wallet consent is required before transfers, not before earning starts.
Registration signing wallets are not automatically payout destinations.
