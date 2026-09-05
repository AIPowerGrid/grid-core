# Worker setup deployment

## Production release

Core `d348f0799f89cf8253b8341c5b3df59ef0151597` was deployed on
2026-09-05 at 11:11:51 UTC, replacing `96b5cf24f4377622c3eed1cb965894ffb00a47ba`.
Both public health endpoints reported the approved commit. All eight workers
connected before the restart reconnected afterward; Redis remained healthy.

The release installs the worker-only `GET /v1/workers/self` status endpoint,
`POST /v1/workers/self/canary`, and aggregate operator setup metrics. This is
setup connectivity evidence with no validator or economic authority.

The candidate's locked dependency installation and `pip check` passed. Its
dependency lock, systemd units, and base Nginx configuration were identical to
the prior release and the live configuration. A fresh production backup was
restored into a generated scratch database; candidate Alembic upgrade and
schema-drift checks passed at `0034`, and the scratch database was dropped.
Production was already at `0034`, so no production migration was necessary.

The cutover preserved the production environment byte-for-byte and retained
the enabled/active payout and backup timer states. Rollback remains the retained
`96b5cf24` immutable release; no schema downgrade is needed for that rollback.

## Supervised endpoint checks

Each check used a temporary, expiring `worker.connect` key bound to one
first-party worker. The public self-status endpoint returned `200`; the same
key could not read account payouts (`403`). Every key was revoked after its
check, and a subsequent self-status request returned `401`.

| Lane | Model | Result | Duration |
| --- | --- | --- | --- |
| Text | qwen3-27b | Incomplete answer: exhausted the 32-token test budget in reasoning | 0.5 s |
| Image | Krea 2 Turbo | Verified media output | 7.4 s |
| Audio | ace-step-v1.5-xl-turbo | Verified media output | 22.3 s |
| Video | LTX Director 2.0 | Verified media output | 42.5 s |

Each request produced one uniquely identified setup job. Queries against those
exact job IDs found zero completion-ledger rows and zero credit reservations.
The media outputs were checked by Core's normal self-canary output path. These
results establish connectivity, not model quality or public media qualification.

## Text release follow-up

The Qwen result exposed an insufficient setup-test budget: its response ended
with `finish_reason=length`, 32 reasoning tokens, and no visible answer. The
production release still has that limit. Do not call its text canary generally
ready for reasoning backends or publish a dependent worker release on that
basis.

The follow-up allows up to 512 completion tokens while retaining the 256-character
visible-answer cap, exact randomized-answer comparison, five-minute worker
cooldown, and no economic effect. Budget exhaustion has its own failure reason.
Shipping that follow-up is a separate immutable Core release from the approved
`d348f079` deployment.

A supervised candidate check also found GPT-OSS interpreting the old word
"token" as a credential request and refusing with Unicode punctuation. The
follow-up describes a public generated test label and compares UTF-8 bytes so
non-ASCII responses remain ordinary mismatches rather than scorer exceptions.

The revised candidate passed a separate supervised check against qwen3-27b
in 1.0 seconds, with zero ledger rows and reservations for its exact job. That
check loaded the candidate only in an operator process and exercised the
deployed transport; it did not replace the production application module.

Local service/router verification passed 881 tests with 73 environment-dependent
skips. The focused self-status, canary, and worker-transport subset passed all
38 tests. Production route checks preserved unauthenticated `401` responses,
the public introspection `404`, and the retired API `410`.
