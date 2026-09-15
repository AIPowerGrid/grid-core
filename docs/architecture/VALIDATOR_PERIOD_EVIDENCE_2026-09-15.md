# Validator Period: Evidence and Remaining Work

Status: **interim investigation, not completed security qualification**.
Deadline: September 22, 2026. Production now runs Core `64d38951` / Alembic
`0042`, with routing observation, model fidelity, media validation and worker
penalty authority disabled. Compensation campaigns are separate contracts;
this audit changes neither their membership nor budgets or historical votes.

## Frozen Failure Cohort

At `2026-09-15T20:48:06.929381Z`, a repeatable-read, read-only PostgreSQL
snapshot preserved 33 finalized failed groups created within
`[2026-09-14T20:46:52.274079Z, 2026-09-15T20:46:52.274079Z)`.
The moving query still contained 33 groups when captured. Subsequent finalized
groups must not enter this denominator. Queries were bounded to 100 groups,
1,000 assignments and 1,000 signed votes with a statement timeout.

- 165 retained assignments, 164 authoritative signed reports.
- All 164 signatures, signed bindings and envelope hashes independently verified.
- All 165 response/evidence commitments and assignment disclosure seals verified.
- Exact released preview.20 source `c73a284f155e86358f5348ab017747cd51d02e58`
  reproduced all 165 stored verdicts. This is retained-evidence replay, not new
  worker execution or an independent ground-truth oracle.
- Five individual replies passed despite belonging to failed groups.
- A separate integer parser verified all 25 multistep-logic answer commitments.

Private capture SHA-256:
`7abf724304059989e7e9d1082a34fbcf94a6b02fca788c386a54bd2ea7050d58`.
Raw synthetic prompts, replies, signer/account bindings and signatures remain
private. Case numbers F01-F33 follow snapshot group creation/id ordering.

| Reported model | Failed groups | Retained observations |
| --- | ---: | --- |
| Smollm-135m | 23 | 115 malformed task outputs across nine capabilities |
| deepseek-v4-flash-nvfp4 | 5 | Tool failures, empty stop outputs, reasoning-only token-limit replies; three individual passes |
| gpt-oss-120b | 4 | Five empty stop replies; 14 reasoning-only and one malformed token-limit reply |
| qwen38-flash-next-125b-nvfp4 | 1 | Three wrong integers under a verified oracle; two individual passes |

These names are advertised identities, not verified weights. Malformed answers
alone do not distinguish weak models, broken backends, transport defects or fraud.

## Direct Owned-Backend Controls

Seven representative cases were selected by reported model/capability. Six
resolved to configured owned backends; the Qwen38 case did not. Fourteen bounded,
sequential HTTP calls compared original streaming/non-streaming requests and two
additional no-stop controls. They bypassed Core, used original budgets, did not
submit attestations and generated no Grid credit or reward records.

| Comparison | Observation | Interpretation |
| --- | --- | --- |
| DeepSeek tool call, F01 | Streamed reply passed; non-streamed reply had invalid JSON arguments | Confirmed backend API defect; not solely Grid stream assembly |
| DeepSeek tool chain, F29 | First stage had invalid JSON arguments in both modes | Confirmed backend API defect; neither reached stage two |
| DeepSeek/GPT-OSS stop cases | Original requests had no visible answer; no-stop controls emitted marker and answer | Reasoning/stop interaction; not proof of fraud or permission to accept empty output |
| DeepSeek/GPT-OSS token-limit cases | Both modes exhausted the budget in reasoning | Task failure reproduced; native-tokenizer violation or substitution not established |
| Qwen38 logic case | No configured owned backend found by this helper | Retained wrong-answer evidence only; direct reproduction open |

Private direct capture SHA-256:
`117a0616539465d49890318a03616ecd1eb38b1ec274afe17e2a29038636194d`.
These are repeated controls on six cases, not 14 independent trials. No-stop
controls intentionally change the task and cannot count as scored successes.

The private per-group ledger labels F01 and F29 `genuine_worker_defect` based
on representative direct API reproduction; both need backend repair and
requalification. The other 31 are `inconclusive` at root-cause level, with
specific retained symptoms. None is labelled model fraud. This is preliminary
triage, not completion of the reproduction and repair requirement.

## Routing Experiment Defects

Read-only production preflight found two integration defects:

1. New-run policy defaults freeze preview.13 even when deployment selects a
   different baseline. Reviewed public operators run preview.20.
2. The evidence query requires completed shared group execution. V8 text runs
   independent assignments, leaving group `probe_status=not_started`; therefore
   valid current text evidence is discarded.

Candidate fixes freeze configured defaults while rejecting explicit mismatches,
and admit only exact-v8 text batches with completed, fully bound supporting
assignments. Legacy/media shared execution, signatures, nonce, account, current
signer, version, freshness and independent-control requirements remain.
An unfinished assignment cannot satisfy independent quorum. The operator CLI
also uses database-enforced read-only previews/reports and never bootstraps
schema, including in apply mode.

Tests first reproduced both defects. A read-only candidate query against live
data with a proposed preview.20 policy found five reviewed participating
operators and 52 finalized independently supported groups, versus zero in the
deployed evaluator. This is **candidate evaluation**, not an active experiment.

The shadow correction passed required CI and deployed at 21:38:17 UTC; see the
[rollout record](../../deploy/VALIDATOR_PERIOD_ROLLOUT_2026_09_15.md), including
the separately held tool-choice candidate and single-replica capacity limit.
Remaining gates: explicit baseline transition
with upgrade overlap removed, protected HMAC secret, collector readiness and an
unresolved stale-candidate critical alert. Diagnostic verification booleans were
left false pending candidate proof review. Do not delete or reject an operator
merely to clear the gate, or infer permission to activate routing.

## Adversarial Controls

Added 20 paired randomized synthetic cases each for malformed tool arguments,
truncated repetition output and ignored stop markers. All 60 correct template
controls passed and all 60 injected faults failed. This measures scorer behavior
on deterministic actors, **not real-model false-alarm rate** or end-to-end
transport detection. Existing tests still demonstrate that a regex solver and
probe-aware backend switch can pass public templates without a useful model.

Still required: isolated actual worker/backend fault injection, honest engine/
quant baselines, deliberate smaller-model substitution, fabricated logprobs and
probe-aware switching. Preserve the existing negative fidelity studies; do not
relabel them as successful detection.

## September 22 Deliverables

### Collector Rejection Accounting

Pre-activation inspection found that malformed or contradictory outbox events
were acknowledged and deleted after only a log warning. The aggregate report
could still claim zero observer errors. A new regression reproduced this for
malformed route, outcome and unknown-kind events against the actual SQL writer
and report, before changing the collector.

The candidate fix durably records `persist/invalid_outbox_event` before
acknowledgement. If that write fails, the event stays pending for reclaim.
Existing observation bindings take precedence for late contradictions; payload
and Redis insertion times provide fallback attribution, never current retry
time. Events outside any running experiment remain non-evidence. No raw event,
identity or exception text is persisted as an error. Retries after an error
commit may append another error; counts are rejection attempts, not unique
events. This conservatively prevents a clean report rather than changing any
worker verdict or production route.

Local verification: 100 focused tests passed, including disposable PostgreSQL
and a private Unix-socket Redis. Required CI and deployment of this correction
remain separate. Observation stays disabled until this and the existing cohort,
runtime, secret and collector gates are qualified.

Candidate verification: 89 focused tests passed on local Python 3.13 and a
disposable PostgreSQL 14 database, including concurrency/replay, v8 evidence
selection, unfinished-assignment exclusion, read-only CLI cleanup and hostile
scorer controls. The database was dropped afterward. Required PostgreSQL 16 /
Python 3.12 CI and full deployment qualification remain separate gates.

- Close the F01-F33 ledger with reproduced causes or explicit unresolved limits,
  actual backend fixes and regression controls.
- Report paired honest/faulted workload counts, detection and false-alarm rates
  by fault, engine and model; distinguish synthetic actors from real models.
- Start the seven-day observer only after every gate passes. It evaluates
  same-model replica preference, not exact scheduler replay. Archive coverage,
  independence, recommendations and actual outcomes without changing routing.
  A later start means a later finish: report partial evidence honestly instead
  of shortening, extending silently or backdating the frozen experiment.
- Publish coverage, corroborated findings, fixes, disagreements, evidence gaps
  and reviewed allocation results. Allocations cannot finalize before campaign
  end plus receipt grace. Consent and transfers remain separate; never call a
  proposed allocation a sent payment.

No additional compensation allocation or transfer is authorized by this report.
