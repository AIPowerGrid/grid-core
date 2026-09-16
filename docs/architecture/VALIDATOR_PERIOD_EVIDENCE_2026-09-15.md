# Validator Period: Evidence and Remaining Work

Status: **interim investigation, not completed security qualification**.
Deadline: September 22, 2026. Production now runs Core `d1aafcf4` / Alembic
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

A later original-request control reproduced F18: its streamed tool arguments
were invalid JSON and byte-identical to the retained failed observation, while
the non-streamed reply passed its committed answer. An independent parser
checked raw SSE/JSON, request equality and commitments without importing Core
or the node scorer. Private capture SHA-256:
`74deef8d7474266219d8fca65f003a89a0128020cc0ed1ad96afbc7791e6c52b`.
These are two additional calls on one case, not an expanded failure cohort.

The preserved original private ledger and its F18/F27 addenda now label F01,
F18, F27 and F29 `genuine_worker_defect` based on representative direct API
reproduction. F27 has a confirmed stop-exclusion contract defect, but that does
not fully explain its missing visible answer (see the GPT follow-up below).
DeepSeek's three cases now have a live parser repair and direct requalification
(see the September 16 follow-up); fresh independent confirmation remains open.
GPT's repair is not deployed. The other 29 are
`inconclusive` at root-cause level, with specific retained symptoms. None is
labelled model fraud. This is preliminary triage, not completion of the
reproduction and repair requirement.

### Case Dispositions

These are root-cause labels, not changes to the retained failed task verdicts.

| Cases | Classification | Next proof needed |
| --- | --- | --- |
| F01, F18, F29 | Genuine worker/backend API defect | Live repair and direct original-task regressions pass; fresh independent validator confirmation pending |
| F27 | Genuine worker/backend stop-exclusion defect | Live stop repair; visible-answer task failure remains separately unresolved |
| F15 | Inconclusive | Reasoning-aware stop controls without excusing empty visible output |
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
The later runtime inspection and repair below supersede the access blocker.
Do not rewrite all customer requests, strip leaked markup, or weaken scoring.

### DeepSeek Live Parser Repair: September 16

Inspection of the actual serving image identified two parser problems: native
DSML tool output was routed through generic named/required JSON handling, and
incomplete tool markup leaked into visible content. Installed serving-generator
tests also exposed terminal-chunk corruption of complete multiple tool calls.
A source/version-pinned plugin repairs these paths without changing request
budgets, tool selection, model weights or expected-answer commitments. Its
terminal handling override applies only to the selected candidate parser class.

After explicit approval of a brief DeepSeek interruption, only that backend
container was recreated at 02:19 UTC. The original image was retained; the
reviewed configuration difference consists of parser selection, plugin flag and
read-only plugin mount. Health returned 200 after 286.8 seconds. No shared worker
bridge, GPT/Qwen backend, Core, scoring policy or economic control was changed.
Private configuration backups and rollback procedure were recorded; rollback
was not executed and is not claimed production-tested.

| Qualification | Result and limit |
| --- | --- |
| Native parser compatibility | 38/38 synthetic text cases passed |
| Actual serving boundary replay | Same oracle improved from 22/24 to 24/24 candidate cases; baseline 4/24 |
| Packaged plugin | 24 boundary cases plus registration, source mismatch, selection and cleanup checks passed, CPU-only |
| Live F18 stream/full | Both original task contracts passed; native output-token IDs and logprob IDs aligned with usage (149 and 134 tokens respectively) |
| Live multiple tools and ordinary text | Two additional bounded calls passed |
| Live original F01 and F29 | Single call and full two-stage chain passed in both stream/full modes, six HTTP calls, unchanged challenge commitments |

These overlapping checks are not independent sample counts. The live calls used
our configured backend directly, not public assignment/quorum or paid Grid jobs.
They created no Grid reward/credit records and do not rewrite the frozen failed
verdicts. New authoritative evidence must come from fresh assignments.

Deployed plugin SHA-256:
`477ccd5b8cca73839228b80432e2056aef17476a80e52039732e38bcb0020e7e`.
Initial live capture:
`9ae4eb2ffe32234b4d25a926fbd11099a1106f8c1ee1c5359b61c121a05be4d1`.
Original single-call/chain follow-up:
`b28fd01746ab7173f905dbfe21b85a1d19ed365185e87e353b48bf375e5035ca`.

Remaining limitations:

- The CLI loads the plugin twice; the second import logs duplicate-registration
  rejection. Exact loader tests verified the first registration and scoped
  terminal policy remain intact. Startup is functional, not warning-free.
- Stream-disconnect experiments returned to idle but did not establish a
  request-specific engine abort. Do not count idle metrics as cancellation proof.
- Whole reasoning-plus-tools in one synthetic batch loses reasoning in both
  baseline and candidate. That separate generic handoff behavior is not fixed;
  its live occurrence rate is unknown. Tested live responses retained reasoning.
- Without explicit returned token IDs, installed streaming buffering can omit
  logprob frames; replay proves an unchanged ordered subset, not lossless traces.

### GPT Backend And Active Worker Follow-Up

The running bridge was identified through its actual process command and
systemd override, not its working directory. It runs the immutable text-worker
v0.3.8 release, which already forwards native logprobs. An initial private
diagnostic had inspected an unused older checkout and incorrectly attributed
missing transport to the deployed worker; that conclusion is withdrawn. Replay
through the actual active handler preserves all 38 logprob-bearing frames in
the retained three-backend capture. This is handler replay, not a new public
Grid job or model-possession proof. No worker upgrade was performed for it.

The owned GPT backend's active interpreter and launch flags establish vLLM
0.10.2 with its native Harmony/OpenAI tool parser. Four original F27 controls
(stream/full crossed with stop inclusion on/off) show the excluded marker in
reasoning even when exclusion is requested. The engine trims its text output,
but Harmony serving reconstructs output from untrimmed token IDs. Explicit
stop-control capture:
`364d2cca01b270ae4d9eaf613359e48d3900bacb1670090c70811d69337e9513`.

A private, version/source-hash-pinned repair candidate now has these checks:

| Evidence | Result and limit |
| --- | --- |
| Initial installed-generator stop fixtures | 12/24 contract checks failed before candidate integration |
| V3 text/channel fixtures | 108 expanded cases and 24 original cases passed; synthetic engine outputs |
| Real protocol V3 follow-up | 44/50 candidate cases passed; exposed lost echo prefixes and secondary error-stream exceptions |
| V4 actual serving/protocol classes | 50/50 candidate cases passed, paired with 50 baseline cases; multiple choices/tools, channel transitions, echo, engine errors and cancellation |
| Explicit in-process install/remove | Same 50 candidate cases passed; source mismatch and duplicate install rejected; original methods restored |
| Two fresh model responses | 43/132 native tokens for stop/no-stop controls; both HTTP 200 |
| Fresh native capture replay | 6/6 full/stream replay variants passed expected text and usage; replay chunk boundaries are synthetic |

These are successive, overlapping studies, not independent sample counts to
sum into a detection rate. The real-schema tests instantiate installed request,
engine-output and response types without starting another engine or initializing
CUDA. Their probability data are synthetic. The fresh response replays instead
use genuine token IDs/probabilities captured from the owned live model.

Fresh native capture:
`a19d49fd66b22060b13f4ac9a67f1bb69898b74bd83c75eaf5cf42647fe9e3c5`.
Full replay preserved 43/43 and 132/132 native probabilities; stream replay
preserved the emitted native ordered subsequences, 41/43 and 125/132. Existing
serving behavior omits some header/control tokens from streams. Do not call
these streams lossless complete token traces or infer model identity from them.

The repaired stop replay still has **zero visible answer characters**: the
model encounters the stop during reasoning. Its no-stop control has 41 visible
characters. Fixing marker leakage is not the same as passing the original task;
no historical verdict has been changed or forgiven.

**Not deployed.** The hook ran only in separate CPU test processes, never in
the serving process. Its source hash is
`5cea76717b40897c06fcf8bc59aaa851991b63bb30c4bf885b630590279b9c89`.
The production backend was not restarted. A supervised live canary, a verified
drain/rollback plan and remaining compatibility review precede any installation.
The shared bridge loads backend configuration once; no per-backend hot-drain
has been established. Core's worker `maintenance` column gates reference/audit
selection, not ordinary worker queue consumption; setting it is **not** a safe
customer-job drain. Do not use health eviction/penalties as maintenance controls.

#### GPT Compatibility Follow-Up

An additional native-tokenizer/installed-serving study found a regression in
the unreleased V4 candidate: six of 18 cases raised on auxiliary commentary,
summary or confidence channels preceding the final answer, including ordinary
requests without a stop string. The original backend returned a final answer
for those no-stop full responses. This negative result is retained; the earlier
50-case result did not establish universal compatibility.

V5 maps analysis/final into their public response fields and leaves auxiliary
channels unexposed, matching the installed streaming implementation. Native tool
extraction and probability metadata are unchanged. The same 18-case oracle now
passes, as do 72 expanded cases checking full-response reasoning with inclusion
on/off, the original 50 serving/install cases, and six original captured-token
replays. These are overlapping CPU studies, not new live generations.

Adapter SHA-256:
`7cac2bffbe44d7415d732127e9199e62d06804886b02aaeb7cbc35916890af05`.
Revised hook:
`4dbb1ce1c07798c7894a02454310a181b1fb4b2d89df952f7d76b04430e8389b`.
The opt-in checksum-pinned launcher passed five boundary tests and an actual
file-based preflight using the serving interpreter without initializing CUDA.
Temporary test files were removed; the running service and its configuration
were unchanged. **Still not deployed:** the prior maintenance approval covered
DeepSeek, not GPT. Exact service override/rollback validation and the separately
approved live canary remain. The original F27 visible-answer failure is still
distinct from stop-marker exclusion, even after repair.

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
unresolved stale-candidate critical alert. The initial diagnostic verification
booleans were false pending proof review; that code-proof review has since
completed for exact deployed Core `d1aafcf4`. It does not satisfy the separate
runtime/cohort gates. Do not delete or reject an operator merely to clear the
gate, or infer permission to activate routing.

At `2026-09-16T00:05:31.746997Z`, a fresh database-enforced read-only preflight
still found five reviewed participating operators and 47 eligible finalized
groups with the proposed preview.20 baseline. Migration, PostgreSQL concurrency,
replay and no-side-effect verification booleans were true, bound to exact
deployed code and CI evidence. The proposed start evaluation failed only
`cohort_monitor_clear`; the cohort remained critical. Production still has
baseline preview.13, the old upgrade overlap, observer disabled and no observer
HMAC configured. Code proof is not runtime activation. The verification artifact
hash is `40a18042b1b60d08494a5fc14a1f6e19f7d79adb3ab9034d753b3643ce89502e`.

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

### Midperiod Compensation Readiness

At `2026-09-16T02:54:30.337663Z`, a database-enforced read-only, repeatable-read
snapshot checked the two existing campaigns using the exact deployed work
verifier. All five frozen members had countable work:

| Campaign | Members | Signed reports verified and countable to date |
| --- | ---: | ---: |
| Original | 3 | 82 |
| Budget-linked supplement | 2 | 53 |

The 135 reports passed signature, account/signer, assignment/nonce, evidence,
target, task-score and timing checks. There were no exclusions, duplicate-work
claims, daily-cap rejections or hard verification errors in this snapshot.
Frozen membership and current reviews matched; the supplement's budget link
revalidated. Both campaign commitments remained unchanged.

This is **not** an allocation or a payment estimate. Correctly reporting a task
failure can qualify as operator work, so 135 must not be described as 135 worker
security successes. Allocation, work-claim, recipient and payment tables all
contained zero rows at capture time. No finalization or transfer was attempted.
Final eligibility and amounts must be recomputed after the actual campaign end
and receipt grace, no earlier than **September 22, 2026 at 15:30 UTC**. Payment
consent and transfer review remain separate.

Private aggregate capture SHA-256:
`daf1de15c76f8dd6b4e4986ab34b80170f2dd57a0c143d2c07e1bab4f8430863`.
Production verifier source SHA-256:
`469ec6512f6a8c2d37c3acae35dd988b23151192b4b4ff02a682b4440ff71660`.

### Collector Rejection Accounting

Pre-activation inspection found that malformed or contradictory outbox events
were acknowledged and deleted after only a log warning. The aggregate report
could still claim zero observer errors. A new regression reproduced this for
malformed route, outcome and unknown-kind events against the actual SQL writer
and report, before changing the collector.

The deployed fix durably records `persist/invalid_outbox_event` before
acknowledgement. If that write fails, the event stays pending for reclaim.
Existing observation bindings take precedence for late contradictions; payload
and Redis insertion times provide fallback attribution, never current retry
time. Events outside any running experiment remain non-evidence. No raw event,
identity or exception text is persisted as an error. Retries after an error
commit may append another error; counts are rejection attempts, not unique
events. This conservatively prevents a clean report rather than changing any
worker verdict or production route.

Local verification: 100 focused tests passed, including disposable PostgreSQL
and a private Unix-socket Redis. PR197 and exact-main CI passed: 2,114 tests,
12 skips, a separate two-test Redis proof and one PostgreSQL/Core/Console/node
handoff test. After a fresh production backup/restore proof, `d1aafcf4` deployed
at 22:27:13 UTC with all nine workers returning and no environment or payout
control changes. Observation stays disabled: deployment alone does not satisfy
the existing cohort, runtime, secret and collector activation gates.

Shadow-evidence correction: 89 focused tests passed on local Python 3.13 and a
disposable PostgreSQL 14 database, including concurrency/replay, v8 evidence
selection, unfinished-assignment exclusion, read-only CLI cleanup and hostile
scorer controls. The database was dropped afterward. The deployed `64d38951`
then passed full Python 3.12/PostgreSQL 16 CI (2,103 passed, 11 skipped), a
separate cross-repo handoff check and production restore/migration proof.
The collector correction has its own verification above; neither release
qualifies the held tool-choice proposal.

- Close the F01-F33 ledger with reproduced causes or explicit unresolved limits,
  actual backend fixes and regression controls.
- Report paired honest/faulted workload counts, detection and false-alarm rates
  by fault, engine and model; distinguish synthetic actors from real models.
- Start the seven-day observer only after every gate passes. It evaluates
  same-model replica preference, not exact scheduler replay. Archive coverage,
  independence, recommendations and actual outcomes without changing routing.
  A later start means a later finish: report partial evidence honestly instead
  of shortening, extending silently or backdating the frozen experiment.
  As of September 16 UTC the run has not started, so its full seven-day outcome
  cannot be available for the September 22 report. That report must disclose
  the shortfall; it does not move the compensation campaign's end or grace.
- Publish coverage, corroborated findings, fixes, disagreements, evidence gaps
  and reviewed allocation results. Allocations cannot finalize before campaign
  end plus receipt grace. Consent and transfers remain separate; never call a
  proposed allocation a sent payment.

No additional compensation allocation or transfer is authorized by this report.
