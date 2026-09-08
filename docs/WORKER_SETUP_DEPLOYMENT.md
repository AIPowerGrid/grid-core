# Worker setup deployment

## Initial production release (2026-09-05)

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
Separate storage HEAD checks returned `404` for each of the three exact
temporary objects after completion, confirming cleanup.

## Text release follow-up

The Qwen result exposed an insufficient setup-test budget: its response ended
with `finish_reason=length`, 32 reasoning tokens, and no visible answer. The
September 5 release still had that limit. Do not call that text canary generally
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

The revised candidate passed separate supervised checks against qwen3-27b
in 1.0 seconds and gpt-oss-120b in 1.6 seconds, with zero ledger rows and
reservations for their exact jobs. Those checks loaded the candidate only in
an operator process and exercised the deployed transport; they did not replace
the production application module. Retests respected the existing worker
cooldown; no rate-limit key was cleared to run them.

Local service/router verification passed 881 tests with 73 environment-dependent
skips. The focused self-status, canary, and worker-transport subset passed all
38 tests. Production route checks preserved unauthenticated `401` responses,
the public introspection `404`, and the retired API `410`.

## Reasoning-budget production follow-up (2026-09-08)

Core `0b4630c555ac10be6a64b5610b493ca38c0730ac` supersedes
`a754b6898b4a6111758b104ee633de18ef0fe252` in production. It includes
the intervening billing safeguard `c40232b2`; no older billing or validator
implementation was restored. The 512-token budget, public-label wording,
UTF-8 comparison, and distinct budget-exhaustion result are now live.

The candidate passed required CI: 1,435 Grid tests, nine environment-dependent
skips, PostgreSQL backup/restore and schema parity, and the real
Core/Console/validator compensation handoff. A fresh production backup was
restored into a generated scratch database and checked against the immutable
candidate. Alembic remained at `0039`; no production migration was required.
Hash-locked wheel installation and dependency consistency checks passed.

Both public health endpoints report the exact deployed SHA with Redis healthy.
All 14 pre-cutover connections, including the isolated release-test worker,
reconnected. Ollama protocol probes delayed some reconnects while models warmed.
The environment remained byte-for-byte unchanged; unit and Nginx assets matched.
The payout timer stayed disabled/inactive, its service inactive, and the backup
timer enabled/active. The preceding `a754b689` release is retained for rollback.

Frozen worker v0.3.9 qualification is tracked in the worker release notes,
separately from this Core deployment. An isolated, unfunded operator-provisioned
fixture uses a short-lived `worker.connect` key and cryptographically verified
delegation. This is not a claim that a new human Console wallet-approval flow
was exercised. Its first Qwen 1.7B test appended `/think` to the correct label
and was correctly rejected as `output_mismatch`; there were zero completion
ledger rows or billing reservations, and the worker key could not access
account payouts. Do not turn a successful connection or correctly rejected
answer into a successful model-fidelity claim.

The warm retry reproduced that mismatch. Ollama's installed Qwen template
appends `/think` to the user message, making an undelimited end-of-message label
ambiguous. The follow-up encloses the public label in explicit tags and places
the reply instruction after it. Three direct local Qwen 1.7B checks passed with
fresh labels and 169-186 completion tokens each. This is backend diagnostic
evidence, not yet proof of the follow-up's deployment or frozen Grid round trip.
The exact comparator still rejects appended `/think`, tags, or commentary.
