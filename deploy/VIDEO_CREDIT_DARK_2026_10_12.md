# Credit-First LTX-2.5 Dark Deployment

## Source And Approval

The sole owner confirmed model use, pricing and launch approval on October 12.
This is the owner assertion, not an independently verified licensor grant or
legal opinion. The first tariff is USD 0.020 per effective output second for
`LTX-2.5 Distilled BF16`. Dev/Ingredients remain unpriced/non-serving.
The next canary is capped at USD 1.00 total holds against existing funded owner
credit; no balance grant, transfer or canary spend was made in this deployment.

[PR217](https://github.com/AIPowerGrid/grid-core/pull/217) merged by the
repository's supported squash method at
`f3e05bf1c35085d128914440d6bbd338ba3f172f`. Reviewed/tested source
`b053b2cec1d330c9d715c532bb4b23a437f62f1c` has exactly the same file tree
as that main commit, verified with an empty Git tree diff. Production selects
the immutable tested `b053b2ce` release and reports that exact commit, not the
different squash commit. The later x402/payment/refund stack was not merged,
deployed or activated.

## Qualification

Exact-source Linux/Python 3.12/PostgreSQL 16
[run38298481524](https://github.com/AIPowerGrid/grid-core/actions/runs/38298481524)
passed every required step:

- 2,835 Grid tests, 26 explicit skips and 139 inherited warnings.
- All four actual pinned media-worker adapter cases, zero skips.
- 56 actual HTTP/SQL SDK cases and the real Console/node handoff.
- Thirty codec cases, two Redis cases, frozen dependency audit and lock parity.
- Backup/restore and schema parity through Alembic `0045`.

CodeQL, reachable-history secret and infrastructure-string checks passed.
Local focused PostgreSQL 14 testing passed 278 with four SQLite-only concurrency
skips; their real PostgreSQL counterparts executed, as did all four worker
adapters. The disposable cluster was stopped. These checks do not prove a
funded native GPU/storage canary.

## Production Procedure And Evidence

The clean immutable candidate was built beside the prior `ca1f2a12` release
using hash-locked binary dependencies. `pip check` passed on Python 3.12.3.
FFmpeg/ffprobe 6.1.1 were installed as required source-decoder prerequisites;
no GPU/model service was restarted and no host reboot was performed.

A verified production backup was restored into a generated scratch database
and upgraded from `0042` through `0045`. Candidate schema parity passed and
the scratch database was dropped. The live database remained `0042` until
the separately applied drained cutover.

The first maintenance verification aborted before stopping Core or changing
schema because the immediate nginx reload probe did not observe the gate.
Cleanup removed only the new maintenance overlay and preserved the live
release/services. A bounded reload wait corrected the check. The successful
attempt required both origin HTTP 503 and the exact expected JSON maintenance
response through the verified public HTTPS API before stopping anything.

Public generation/probe ingress was gated, MCP stopped temporarily, and two
quiet checks plus a stopped-Core check found zero held reservations, zero
pending/actual undelivered jobs and zero running pinned shadow observations.
The historical stream lag counter was not reset. A fresh production backup
was verified before live migrations `0043`/`0044`/`0045` applied.

At `2026-10-12T20:07:03Z`, the selected release reported healthy Redis, all six
workers and the same eight-model fleet. Ingress reopened after readiness;
MCP resumed and nginx configuration validation passed. Post-cutover checks
confirmed:

- Public `/health` reports the exact `b053b2ce` source, six workers and Redis.
- Public `/v1/pricing` reports book `2026-10-12-a` and the new Distilled tariff;
  LTX-2.3 and all other customer rates remain unchanged.
- Alembic is `0045`, and `grid_media_jobs` contains zero rows.
- Core/MCP and the existing payout/backup timers are active; timer enabled/active
  states match preflight. Core automatic restarts are zero.
- The service environment is byte-for-byte unchanged. Global charging remains
  `on`; existing generation paths, economics and compensation are unchanged.
- Async video admission is disabled, mode `pilot`, with no admitted accounts.
- An otherwise valid unauthenticated async-video request returns 401; validator
  assignments remain 401 and the retired heartbeat remains 410.
- The temporary maintenance overlay is absent. The protected client GPT service
  and other GPU/model services were untouched.

Implementation features in health are not public model/admission qualification.
The serving/default video model remains LTX-2.3; no LTX-2.5 worker was enrolled.

## Remaining Gates And Rollback

The owned LTX rig needs fresh Tailscale SSH authorization. Public worker
enrollment and the purchased-credit canary have not run. Use the actual funded
owner-account session and preserve original job/request identities on reply
loss; do not replace this with seeded test credits or another generation POST.

Production still has no configured separate private-source bucket. Anonymous
denial, public-endpoint settings and one-day lifecycle must be verified before
image/audio/video source admission. Decoder installation does not close that
storage/privacy gate. Dev/Ingredients, public broad admission, the Gallery/
agent release and actual video x402 collection/refunds remain separate gates.

Admission rollback closes new work but retains existing-job recovery, outbox,
settlement and source cleanup. Once async history exists, preserve migrations
`0043`-`0045` and lease/journal-aware handlers; never downgrade to a release
that forgets accepted jobs or manufacture another render. The prior release
remains preserved. Returning to its old handlers is valid only while the new
journal is proven empty; no production rollback occurred after successful cutover.
