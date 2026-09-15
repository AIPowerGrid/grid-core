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

### Case Dispositions

These are root-cause labels, not changes to the retained failed task verdicts.

| Cases | Classification | Next proof needed |
| --- | --- | --- |
| F01, F29 | Genuine worker/backend API defect | Pinned serving/parser repair and full tool-chain regression |
| F18 | Inconclusive | Its own tool-call reproduction; same model name does not establish the same cause |
| F15, F27 | Inconclusive | Reasoning-aware stop controls without excusing empty visible output |
| F03, F07, F24, F32 | Inconclusive | Separate visible-task failure from native-token budget compliance |
| F11 | Inconclusive | Direct owned reference for the independently verified wrong integer |
| F02, F04-F06, F08-F10, F12-F14, F16-F17, F19-F23, F25-F26, F28, F30-F31, F33 | Inconclusive | SmolLM reference/backend controls; malformed output is not proof of substitution |

### Tool Selection Controls

A proposed new-challenge change from named selection to `required` passes local
LM Studio controls, but is **held as draft PR195**, not deployed. Four direct
DeepSeek variants retained the same two original cases and original budgets:
only the single-call non-streamed variant passed. Both streamed variants and
the chain's non-streamed first stage returned no call. Required-choice capture:
`c38fa65f7765c0257b93760a89629a03f3891cad01490a899b0461b537e7219a`.

A separately captured `auto` diagnostic completed five calls: single-call
non-streaming and the complete two-stage non-streamed chain passed, while both
streamed cases leaked native markup into visible content and failed. Capture:
`0940aacbbf724c82f1e9bf815223675e330e9d1d88ede40ea5169206e6c93d1d`.
These mode changes are diagnostics, not retrospective repairs of named-choice
evidence. Upstream reports describe similar
[streaming markup leakage](https://github.com/vllm-project/vllm/issues/40801),
but are not proof of the deployed parser's cause or a qualified patch.
Actual runtime/launch access and pinned backend qualification remain necessary.
Do not rewrite all customer requests, strip leaked markup, or weaken scoring.

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

### Actual Worker Transport Controls

An isolated loopback HTTP/WebSocket/Redis harness used published worker v0.3.9
source `1e5feb38d9ccc5ff94d4c08892f86a31fd48a65b`, Core `cada63c7`, released
preview.20 scoring and actual LM Studio GPT-OSS-20B/MXFP4 responses. Original
named-tool requests received backend HTTP 400; those failed harness/preflight
attempts remain captured separately. A new frozen study changed only the
single-tool choice to `required`, retaining the six original tasks and budgets.
That study does not qualify the held global tool-choice proposal.

| Path | Observed result |
| --- | --- |
| Six actual valid 20B controls | Six healthy; no false failures in this sample |
| Six paired mutations of native outputs | All malformed-tool, ignored-stop and wrong-output cases failed |
| Two additional partial native-output truncations | Both failed |
| Six fresh actual 20B responses advertised as 120B | All healthy: functional canaries missed substitution |
| Six synthetic controls and six synthetic faults | Controls passed; faults failed |

The 32 paths include paired/correlated observations, not 32 independent model
trials. Eight injected native-output faults detected is not a population
detection rate. Two further complete native tool chains (four backend calls)
passed with Core's exact second-stage message construction and independently
verified combined commitments. No public registration, signed quorum, customer
job, reward or credit movement occurred. Public workers were left untouched.

Private manifests: main
`2ebad489a0195007c28d2876f746350ac944665b73d80aaca274a3eb1f69da8c`,
partial truncation
`7aaa12087a49b551d15e60a2e56275468595b3a617f125a90f9dc967e44da8d9`,
complete native chains
`960082950d9677248e827d8a7d663f808681d07a53e93722a3af9e8466474b30`.
The independent auditor rechecked response hashes, commitments, schedules,
budgets, ground-truth answers and source bindings without importing the scorer.

Still required: broader honest engine/quant baselines and a production-relevant
fidelity qualification. The functional substitution miss above is not a test
of the separate logprob comparator. Prior fixed-context batch evidence detects
three eligible cross-model batches with one unavailable batch, but copied
probabilities evade it; earlier answer-logprob rules missed substitutions.
Preserve both positive and negative studies and their coverage limits. No
model-possession claim, probe-aware-switching defense or economic authority
follows from these results.

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

Shadow-evidence correction: 89 focused tests passed on local Python 3.13 and a
disposable PostgreSQL 14 database, including concurrency/replay, v8 evidence
selection, unfinished-assignment exclusion, read-only CLI cleanup and hostile
scorer controls. The database was dropped afterward. The deployed `64d38951`
then passed full Python 3.12/PostgreSQL 16 CI (2,103 passed, 11 skipped), a
separate cross-repo handoff check and production restore/migration proof.
This does not qualify the later collector correction or tool-choice proposal.

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
