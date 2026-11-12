# x402 Minimum Dark Deployment - October 16, 2026

## Result

Core selected immutable `f72ea9379d97d9b6c5fa060ea65555fa659be941`
at `2026-10-16T02:43:10Z`, with Alembic `0046`. It supersedes
`9d8035a823ba0e4b153209bba54605e64b9218b7`. All eight workers returned,
all previously available models remained available, and public generation
reopened after the guarded cutover.

This is a dark x402 deployment, not successful mainnet payment qualification.
`GRID_X402_ENABLED=0`; no new paid canary, wallet signature, token approval,
payment or inference was sent by these deployment checks.

## Source and Verification

- [PR #231](https://github.com/AIPowerGrid/grid-core/pull/231) merged by squash.
  GitHub rejected a merge commit; no branch protection was bypassed.
- All nine checks passed on tested commit
  `1726f130b985d50be9edaf879b5afa865d1e8176`, including the required
  [PostgreSQL/restore/full-suite/client integration run](https://github.com/AIPowerGrid/grid-core/actions/runs/38839186689).
- The complete trees of that tested commit and deployed main are identical:
  `61751922c7d65d03506e25e51492ee3337d433ec`. Main's duplicate full-suite
  workflow was still running at cutover; it is not counted as a completed proof.
- The separately built main release consumed only hash-locked binary wheels;
  dependency consistency and the disabled read-only x402 preflight passed.
- Before selection, a fresh production backup was restored into a generated
  scratch database, migrated with the exact main release to `0046`, checked
  for schema parity and removed by the restore helper.
- Restored backup: 244,293,989 bytes; SHA-256
  `b4dc6655f83341a22ba6b48882a78929be131f6abcbb9c9912d2f5017b873d9c`.
  Another fresh backup was taken after drain, immediately before live migration.
  Backup locations and operator artifacts remain private on the production host.

## Cutover and Postflight

The temporary exact-route generation gate was verified through both origin
and public HTTPS before stopping anything. MCP was paused, accepted work
drained, the final backup completed, and the API was stopped before migration.
After `upgrade head` and `alembic check`, the selected release was changed
atomically. The gate was removed only after worker/model and configuration
checks passed; MCP resumed.

Independent postflight verified:

- Public and origin health/network report the exact deployed commit, healthy
  Redis and eight workers. Models are preserved; automatic API restart count is 0.
- Public models and validator capabilities return 200; assignments require
  authentication (401); retired heartbeat remains 410.
- Public OAuth introspection remains 404. Existing MCP service-key introspection
  works only over loopback; that key cannot read account credits (403 scope).
- A valid unsigned x402 request returns 404 with the disabled-route response.
  Read-only preflight confirms Base 8453, canonical USDC, facilitator support,
  minimum 1000 micro-USD and policy `x402-minimum-2026-10-15-v1`, with zero
  payments and zero inference dispatched.
- Six running API processes preserve protected configuration. Charging stays
  on; existing NVFP4 admission stays on. No pricing, admission, validator,
  promotion or reward control was changed by this deployment.
- API, MCP, PostgreSQL, Redis, payout timer and backup timer are active.
  Nginx/service wiring and protected environment hashes match the baseline.
- All original fields of the existing x402 receipt are unchanged. It remains
  one manual-review row, one attempt, recorded amount 7 micro-USD, no transfer
  hash and payout-ineligible. Its new minimum is 0; migration did not reprice it.

## Preserved Staging Interruptions

The first restore test passed, but its subsequent configuration-preservation
check stopped when the concurrent owner-approved video rollout changed the
environment and selected `9d8035a8`. No cutover was attempted against the stale
baseline. A new baseline preserved the live NVFP4 settings, original wiring
and payment history, followed by another successful isolated restore proof.

After the squash merge, cloning a bundle containing a remote-tracking ref
initially produced an empty staging clone. Checkout stopped before dependency
installation or selection. An initial repair also stopped on the retained
bundle's existence check. The retained bundle was then checksum-verified,
explicitly fetched, and the main release built and restore-tested successfully.
All failed-attempt records were retained; no production restore or ledger edit
was used to repair these staging issues.

## Remaining Gates and Rollback

Ordinary prepaid prices are unchanged. Only new successful x402 requests use
the disclosed USD 0.001 minimum; failures charge zero and historical receipts
retain their original terms. Public x402 stays closed until a separately
approved capped real-worker payment, independent USDC receipt and replay proof
pass. Do not retry the old receipt or use worker payout funds for that test.

The prior immutable release is retained. While x402 remains disabled with no
new minimum-priced jobs, rollback may select it and retain additive schema 0046.
Once any new minimum-priced job exists, preserve minimum-aware terminal and
recovery writers; do not blindly restore the old writer or downgrade history.
Recheck the actual current video/configuration state before any later rollback.
