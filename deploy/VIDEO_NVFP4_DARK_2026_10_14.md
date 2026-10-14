# LTX-2.5 NVFP4 Dark Deployment

This is the initial02:09UTC dark-deployment snapshot. Subsequent worker
enrollment, funded canaries, exporter installation and current closed-cohort
state are recorded in [the paid-retest report](VIDEO_PAID_RETEST_2026_10_14.md).
Its original empty-journal observations are historical, not current state.

## Source And Gates

Core [PR222](https://github.com/AIPowerGrid/grid-core/pull/222) merged as
`53ebf374be91fe3f2dccf3f63a15c0a810efd13e`. Its file tree equals the tested
source `4e9864c8f3fac46b160b5455ccd462cc42fada03`, verified with an empty Git
tree diff. Production selects that exact tested immutable release and reports
`4e9864c8`, not the different squash commit. This supersedes the October12
dark release without enabling a video pilot or x402.

Exact-source Linux/Python3.12/PostgreSQL16
[run38496563870](https://github.com/AIPowerGrid/grid-core/actions/runs/38496563870)
passes all required steps:

- 2,882 Grid cases, 26 explicit skips and 139 inherited warnings.
- All four actual appearance-adapter and two NVFP4 startup cases, zero skips.
- 56 PostgreSQL/real-HTTP video SDK cases and the actual Console/node handoff.
- Thirty source-decoder cases, two Redis cases, dependency audit/lock parity.
- PostgreSQL backup/restore and schema parity through `0045`.

All exact-source CodeQL, complete-history secret and infrastructure-string
checks pass. Worker [PR33](https://github.com/AIPowerGrid/grid-media-worker/pull/33)
merged as `b1fbbec1c2c0f7828e55aeb6cd1935097ff033a9`, tree-identical to reviewed
`629a47cb4833a0089bfb07660c8857159ffe0212`; its three Python suites, packaging,
release gate and security checks pass. These tests are not purchased-credit
or production GPU enrollment proof.

## Native And Storage Evidence

The owned5090 passes official Distilled NVFP4 generated clips at33/81/337
frames, image/audio reference cases and a337-frame960x512 V2V case. All outputs
fully decode; the last V2V case additionally passes exact clocks and reference
waveform checks after a separately bound native export overlay. Prior geometry
and clock-review failures remain private immutable evidence. Original services
are restored and the independent music process is unchanged. The separately
staged dual-GPU exporter passes33/81/337 CPU fixtures with GPUs hidden.
These are the listed local workloads, not all-shape capacity, identity/lip-sync,
worker enrollment or paid Grid acceptance. Director owns the guarded overlay
and detailed private qualification evidence.

The actual production-account source bucket is independently verified through
authenticated provider settings: no custom domains, disabled public-development
access and enabled one-day source/multipart retention. A new tiny source probe
passes signed reads, three-second expiry, fresh-link recovery and exact-object
deletion with independent HEAD404. Unsigned S3 HTTP400 is not public-endpoint
privacy proof. Workers still receive only scoped expiring links; storage
credentials stay on Core.

## Production Procedure

The candidate is built beside the live release using only hash-locked binary
dependencies; `pip check` and clean detached source verification pass.
Early helper PATH/traversal errors stop before cloning/installing and remain
private; correcting those helpers changes no source or live service.

A fresh240,178,740-byte production backup is restored into the runbook's
generated scratch database. Candidate migration/schema parity passes at0045
and that scratch database is removed. No live schema upgrade is required.

Exact generation/probe ingress is gated and independently verified through
origin and public HTTPS before stopping anything. MCP temporarily stops; two
quiet checks, a further fresh backup and a stopped-Core check verify no held
reservations, pending/undelivered work or pinned running observations. The
async journal is empty. No historical stream counters or financial records
are rewritten.

Only `MEDIA_PRIVATE_SOURCE_BUCKET` changes in the service environment. A
structured dotenv update preserves every other effective field and the file's
owner/group/mode, retains a private original and verifies the exact old hash
before applying. Actual running-process environment confirms the new field.
At `2026-10-14T02:09:00Z`, the selected release reports healthy Redis and all
six existing workers with all eight prior models. Ingress reopens, MCP resumes,
and Nginx validation passes. The payout/backup timer states are preserved.
The protected client GPT service and all GPU/model services are untouched.

## Postflight And Remaining Gates

- Public health reports4e9864c8; automatic restarts are zero.
- Public price book2026-10-13-a has distinct BF16/NVFP4 Distilled rates at
  USD0.020/effective generated second. Pricing is not availability.
- Global charging remains on. Async remains disabled/pilot with an empty
  cohort/deadline and zero journal rows. LTX-2.3 still serves.
- Core/MCP and existing payout/backup timers are active; maintenance is absent.
- A valid anonymous request with its mandatory positive ceiling returns401.
  The first fixture omitted that ceiling and correctly returned redacted422.
- The default-urllib public health probe receives a Cloudflare403. Separate
  origin and normal HTTPX public checks pass; no protection is weakened and no
  second restart follows. Retain that client-specific observation.
- No video x402 route is present in deployed OpenAPI. No x402/payment branch,
  transfer, new grant or canary spend is activated by this deployment.

Reviewed workers are staged, not enrolled. Use the actual funded owner session
for the already-approved USD1-total canary; do not substitute seeded credits,
an administrative impersonation or a new Base transfer. Confirm actual debit,
output, same-job crash/reply-loss recovery, failure refund and private-input
cleanup before enabling the bounded pilot and connecting Gallery/Director.
Dev/Ingredients remain unpriced/non-serving. Consumer UI remains unwired.

Rollback closes admission while preserving new handlers, source credentials,
journal/outbox recovery and cleanup. Returning to the prior binary/config is
safe only while async history is proven empty. Once jobs exist, never discard
their execution plans or downgrade to handlers without their recipe identities.
