# Demand billing launch evidence - 2026-09-08

## Posture

IN PROGRESS. Not a global billing activation or permission to resume payouts.
The goal covers every public generation path and all first-party frontends.
Unverified paths must be disabled or fail closed before public charging launch.
Historical accrual and disputed payments are outside this rollout.

## Legacy consumer activation review (2026-09-09, 22:36 UTC)

A read-only production transaction, with a five-second statement timeout,
inventoried active, unexpired stored keys without selecting credentials,
hashes, account IDs, wallet addresses or labels. Counts describe keys, not
people or independent operators:

| Kind | Scope source | Inference policy | Keys |
| --- | --- | --- | ---: |
| Ordinary | Explicit | Canonical account | 73 |
| Ordinary | Explicit | No inference scope | 22 |
| Ordinary | Legacy defaults | Canonical account | 35 |
| Session | Legacy defaults | Canonical account | 30 |
| Service | Explicit | Delegation required | 4 |
| Service | Explicit | Bounded direct service | 4 |
| Service | Explicit | No inference scope | 1 |

Total: 169 keys; 73 used in the preceding seven days. No malformed scope rows
or active keys belonging to inactive service clients were found. One ordinary
inference credential resolves to an account with a request-quota exemption.
That exemption is not a purchased-credit exemption.

The new `test_consumer_activation.py` adds 27 PostgreSQL cases. Persisted
legacy-empty-scope, session and explicit inference keys pass real key lookup,
then durable text/image/video/audio authorization. With global mode selected,
an unmatched account/service/model allowlist and legacy `CHARGING_ENABLED=0`
cannot produce dry-run behavior. Funded quota-exempt accounts get a durable
debit/hold; empty accounts reject without a hold or reward. A repeated terminal
release creates exactly one full refund. Non-inference scoped keys return 403.
The combined consumer/native identity/service-policy/free/promo/mode suite
passed 176 tests locally, including these 27 on PostgreSQL 16.15. Free/promo
availability and holder discounts in the new matrix are zero fixtures; HTTP
dispatch, provider proofs, GPU output and concurrency are not exercised here.

Legacy key ownership and revocation review remains separate housekeeping.
Do not revoke unknown credentials en masse or treat them as an exception to
global charging. This inventory does not prove which third-party app owns a
key, and does not prove live global enforcement while production is allowlisted.
No production keys, flags, balances, payouts or runtime were changed by this
review. The required same-cohort observation and global activation remain open.

## Consumer and payout review (2026-09-09, 21:56 UTC)

- Fresh Chat container inspection confirms API/background/web release
  `085e7394b5-r2`. The running identity assertion, tool constructor and image
  tool source hashes match reviewed commit `085e7394b5`. Its only configured
  image provider (`aipg_krea2_turbo`) now matches both the canonical service
  endpoint and credential. No credentials were printed or changed. An
  authenticated read returned 200 and allowlist charging. The earlier
  mismatched-image-provider warning below is historical; the paid Chat image
  canary already recorded here supplies the charged-account evidence.
- The undelegated Chat credit summary's null service budget is not an inference
  exemption: the bridge has no direct-service-submit scope and must delegate
  inference to a user. Inventory/revocation of old consumer keys remains open.
- Read-only production PostgreSQL EXPLAIN ANALYZE checked the actual account,
  total-DEN and backing-health aggregation statements, with five-second
  statement limits. Last closed hour: execution 6.998/1.875/3.708 ms. Fixed
  reward cutoff through 21:56 UTC: 32.735/10.189/18.618 ms. Those windows had
  one/two eligible accounts and zero unbacked eligible jobs/DEN. These are
  current-load measurements, not a high-volume load test or payment proof.
- Sender review reproduced two defects in local regression tests: accrued and
  pending/failed retry broadcasts skipped sanctions screening, and uncertain
  broadcast errors replaced the persisted candidate hash with exception text.
  The candidate centralizes pre-sign screening, retains nonce/hash evidence,
  and permits proof-only reconciliation of already-mined transfers. No
  screening decision claims to cancel an already-broadcast transaction.
  Real PostgreSQL also rejected the prior `blocked_sanctions` label because
  the status column is VARCHAR(16); holds now use existing `manual_review`,
  preserving the current schema and excluding automatic retries.
- Separate payout blockers remain: period allocations are recomputed on retry,
  no durable overlapping-window guard exists, and historical retry/accrued
  commands select broadly. Example: paying one of two equal recipients 50 of
  a 100-token period, then changing weights from (1, 1) to (1, 1, 2), can
  cause rerun allocations of 25 + 50 for the remaining recipients: 125 total.
  Freeze/reconcile allocations and historical exclusions before any sender.
- Focused PostgreSQL 16 sender lifecycle, allocation and screening verification:
  70 tests passed, including the advisory-lock concurrency test. Eleven new
  cases cover all four sender paths, screen failure, mined-proof-only recovery,
  and retained-hash reconciliation after uncertain broadcast. No Base sends.
  The expanded settlement, PostgreSQL reward-eligibility, multi-asset, revenue
  and screening suite passed 167 tests with no skips. These use simulated
  chain responses, not live transfer validation.

Production is unchanged at `94be0cc1`, allowlisted, with payouts paused. This
review does not restart the 24-hour cohort observation or authorize global
charging, treasury funding, backpay, or a runtime deployment.

## Core process crash proof (2026-09-09, local)

`grid_api/routers/tests/test_core_process_crash.py` adds 27 real Core-process
crash/restart cases. The combined process, worker-failure and Redis-handoff
suite passed **115 tests** against isolated PostgreSQL 16.15 and Redis 8.10.0
on the maintainer machine. Required PR/main CI supplies PostgreSQL 16 and Redis
with the production Python 3.12 dependency lock; local Python 3.13 results do
not replace that gate.

- Each case starts the actual Uvicorn app, authenticates scoped user and worker
  keys, submits HTTP work and drives the worker WebSocket. It SIGKILLs Core
  after the durable reserve, after worker dispatch, or after the atomic terminal
  commit but before success delivery/queue acknowledgment.
- Restart retains the same disposable PostgreSQL database and Redis instance.
  Core's actual startup reclaimer/sweeper must recover the job or refund the
  orphan. Independent connections assert ledger/balance parity, original job
  identity, exactly one charge/reward, and zero reward on duplicate completion.
- Chat, Responses and Anthropic each cover streaming and non-streaming. Image,
  video and audio cover exact-cost settlement and authenticated result recovery
  by job ID and the frontend's original request reference. Audio registration
  and completion use ephemeral real signatures, not an identity bypass.
- A second restart and completed sweep must leave all monetary rows unchanged.
  Storage presigns/presence and generated media/text are fixtures; no GPU or R2
  is contacted. Recovery clocks are shortened. This does not establish Redis
  or PostgreSQL server-crash durability, multi-host failover, output quality,
  external OAuth behavior, or elapsed production observation.

This is tests/documentation only. Production remains `94be0cc1`, charging
allowlisted and payouts paused. It requires no production restart. Earlier
sections describing component-only crash evidence are historical snapshots.

An independently labeled operational alert canary was also delivered through
production's real Redis-backed alert queue and accepted by Discord (HTTP
200/204). The private `alert-delivery.json` beside the release evidence records
the check. This proves transport delivery, not human acknowledgment or an
injected monetary fault; operational response remains open.

## Atomic queue recovery deployed (2026-09-09, 20:03 UTC)

- Core PR167 merged as `94be0cc128d7b2f5a62981549a70cece7eeab8d6`.
  Required CI ran Python 3.12.14, PostgreSQL 16 and Redis 7.0.15: 1,783 passed,
  9 explicit skips, plus the separate real Core/Console/node integration.
  Dependency audit, backup/restore, migration parity, CodeQL and secret scans
  passed. The full objective is not complete merely because CI passed.
- At `2026-09-09T20:03:49Z`, production selected immutable
  `/home/aipg/releases/grid-core-94be0cc1`. A fresh checksum-verified backup
  restored into a generated scratch database and passed Alembic `0040` parity.
  Dependencies, schema, migrations, systemd and Nginx source are unchanged.
  Exact generation routes were gated while one pending text job drained;
  two consecutive empty-queue observations preceded restart. The temporary
  maintenance overlay was removed after health verification.
- Public health and network status report the exact commit, operational Redis,
  nine online workers and all previously tested modality models. The supervisor
  and five children use the new release and preserved configuration: one owner,
  nine models, the same four model-independent direct services, seven enabled
  paths and the exact `2026-09-09T16:26:27+00:00` reward cutoff. Daily-free
  spending remains off, existing promotion/monitor settings unchanged, payout
  service and timer inactive, and Core automatic restart count zero.
- Fresh zero-spend service requests to chat, Responses, Anthropic, image, video
  and audio each returned `402`. Queue IDs, reservation/credit counts and the
  service balance were unchanged. A valid anonymous chat request returned
  `401`. Temporary canary keys were revoked and then rejected by Core.
- The new funded chat job `3f8196a0-0b67-4269-bb27-ca82b3ae5432` returned
  visible output, reserved 40 micro-USD, charged 6 and refunded 34. Exactly one
  committed completion has a result hash and 26.68 purchased-backed eligible
  DEN. No payout was sent. Balance is USD 9.772654; cumulative actual canary
  spend is USD 0.228021 of the approved USD 1.
- Read-only reconciliation of the recorded jobs and global balances passed,
  with zero drift, negative balances, invalid splits or stale holds. Private
  backup, drain, process, canary and audit evidence is retained under
  `/var/lib/aipg-backup/demand-release-94be0cc1/`. Compatible rollback is
  `ad5a1257`; retain schema, the exact reward cutoff, path restrictions and
  stopped payouts. Do not re-enable old retry behavior as a global launch.

- Found and reproduced an acknowledgement-before-append gap in mismatch,
  affinity, generation-failure and stale-recovery paths. Real Redis append
  errors removed the original pending claim in all four paths. Concurrent
  retry callers also duplicated work, and duplicate generation retries could
  incorrectly tell a caller to refund an already-requeued job.
- The release moves each handoff into a pending-checked Lua operation with
  append before acknowledgement. Repeat handoffs are nonterminal no-ops.
  Affinity exhaustion retains the running claim; original progress/targeting
  fields and message-carried retry budgets survive retries and stale recovery.
  Existing legacy generation counters are honored during upgrade.
- Local focused result: 45 passed, including 38 real disposable-Redis cases.
  Sixteen cases kill a real child running Core queue code immediately before
  handoff or after Redis completes it but before the caller gets its result.
  Recovery exposes one executable job on both text and media streams. Initial
  red run reproduced 15 failures against the unmodified implementation.
- This is not a full Uvicorn/worker/PostgreSQL restart or Redis-server crash
  proof. It does not make GPU execution exactly once or close stream retention
  under overload. The production restart above drained work first, so it is
  not an in-flight Uvicorn/worker/PostgreSQL crash experiment. Global rollout,
  remaining identity/alert coverage and the observation window remain gates.
- PR/main CI explicitly installs Redis so these proofs cannot silently skip
  because the executable is absent. Do not deploy a mixed-version fleet and
  assume old retry code honors the new handoff; confirm all Core processes
  run the reviewed release before relying on it; the six-process production
  inspection above verifies that upgrade.

## Worker failure and Console recovery evidence (2026-09-09)

- New PostgreSQL-backed worker tests exercise the actual registration/dispatch
  loop for chat, Responses, Anthropic, image, video and audio. Explicit errors
  refund once; timeout/disconnect exceptions keep the original hold while
  requeued and refund on give-up; partial chat failure cannot create a reward.
  Interrupted refund transactions leave a recoverable hold. The actual sweeper
  restores it once, and a late successful terminal cannot mint a ledger entry.
- Six additional cases kill a real child process after Core updates reservation
  status inside a refund transaction but before its credit write. A separate
  PostgreSQL connection sees no committed terminal change, and the unchanged
  sweeper recovers the hold after process death. Together, 43 new tests pass
  locally against isolated PostgreSQL 16. Local Python is 3.13; required CI
  verifies the production Python 3.12/Linux dependency lock separately.
- These tests simulate authentication, worker frames, Redis queue/registry,
  R2 and client delivery. They do not prove elapsed production timeouts, real
  GPU faults, Redis redelivery after a Uvicorn process crash, or independent
  operator behavior. No live worker was interrupted, no new generation was
  submitted, and no production credit/payout state changed.
- Console PR31 merged as `98e96a4c30f20c6f236049731122c622c0bf680e` and
  deployed as Vercel `dpl_GJ8fKCArScCLqcJyzoAJiLsswt2V`. Its required browser
  test proves receipt reload, 425/503 retries, already-credited completion,
  denied storage and failed cleanup without another wallet payment. The
  production alias, retained owner login and funding history were checked;
  anonymous deposit reads/claims rejected. Console PR32 records the deployment
  and merged as `043a3b49a52d18c31029898196876161bb97540d`. These are fixtures
  plus production read checks, not a second real funding transfer.
- Refreshed Gallery, Music and Console sessions showed the same purchased
  balance with their normal rounding. Core remains `ad5a1257`/0040,
  charging allowlisted, with the prospective reward cutoff and seven-path
  restriction preserved. Payout service/timer remain inactive. Full activation,
  the runbook observation window and remaining negative/alert tests are still
  separate gates. Gallery's earlier recovery PR31 is now merged as
  `7f6bb31d6a1e0c463b15d25520e4321f3f91f80c`.

## Recovery and deposit retry proof (2026-09-09, 17:35 UTC)

- Retried the owner's already-credited Base USDC transaction
  `0x96bc723b567c8ff27bf1a550ab2768b553d8fb0f52885d9f1445cf6d62e74b6c`
  twice through the production deposit-claim API. Both returned HTTP 200,
  `credited=false`, `already_claimed=true`, and original deposit ID `3`.
  Purchased balance remained USD 9.772660; deposit and credit-ledger row counts
  were unchanged. No new transfer or credit was created. The short-lived
  account-read key was revoked and a subsequent credit read returned 401.
  Private proof: `/var/lib/aipg-backup/deposit-retry-20260909/proof.json`.
  This proves live backend idempotency, not the browser's complete failed-
  receipt recovery workflow.
- Fresh browser tabs restored the owner's existing linked-account sessions on
  Gallery, Music, Console and Chat without another login. Displayed purchased
  balances were USD 9.773 / 9.7727, consistent rounding of USD 9.772660.
  Console retained the USD 10 funding receipt. This is one linked real account,
  not an independent Google-only or wallet-only login ceremony.
- Music PR #8 merged as `92d0fba9df1f3a3d87f622e473a71183a3e9ff4c` after
  its production-build auth smoke passed. The new case kills the actual Next
  process with SIGKILL after a local Core stand-in accepts the job but before
  its response arrives. Restart against the same disposable SQLite journal,
  repeat request, and later result recovery preserve the original job with
  exactly one Core generation submission. These are isolated crash tests,
  not a production outage experiment; no runtime deployment was needed.
- Gallery PR #31 adds equivalent real-subprocess crash tests for signed
  Google-only and wallet-only fixture sessions against disposable PostgreSQL
  16. The restarted process preserves the accepted request, reports its
  uncertain internal state as public `processing`, recovers the original Core
  result, and never requotes or submits again. Local `go test -race ./...`,
  `go vet ./...`, and PostgreSQL-backed backend CI passed. Browser CI initially
  failed before tests during dependency installation (Google apt repository
  checksum mismatch); a retry reproduced it. Browser CI now excludes the
  unused system Chrome apt source on its disposable runner, while retaining
  dependency integrity checks and Playwright's own Chromium installation.
  Merge status must be checked separately. These tests use a local Core
  stand-in and fixture JWTs, not live OAuth or a production Core process crash.
- No additional canary spend, production generation, grant, payout or global
  charging expansion occurred in this proof pass. Cumulative actual spend is
  still USD 0.228015 of the approved USD 1.

## Streaming disconnect proof (2026-09-09, 17:13 UTC)

Each request emitted streaming output before the HTTP client deliberately
closed its connection without receiving a terminal. The reservation was still
`held` immediately after disconnect, then settled once at the worker terminal.
This proves disconnect reconciliation, not cancellation of backend generation
or recovery after killing the Core process. Work continued and was charged for
its full measured generation, not merely the fragment the client read.

| Format | Grid job | Reserved micro-USD | Actual micro-USD | Refunded micro-USD |
| --- | --- | ---: | ---: | ---: |
| Chat | `aab200ba-933a-45e6-a124-4f7c62860c1a` | 156 | 154 | 2 |
| Responses | `099e98a0-0316-40c4-960b-6f6acc75b88f` | 156 | 153 | 3 |
| Anthropic | `bdec7055-44de-4aed-a8e8-aef06b456126` | 79 | 69 | 10 |

- Every job has a nonempty output hash, one debit and its unused refund,
  purchased-only settlement, and fully purchased-backed reward eligibility.
  The unchanged read-only canary auditor reconciled all three with no findings,
  no account/global balance drift, negative balances or stale holds.
- The first helper run passed Chat and Responses, then its Anthropic request
  returned 401 because the helper omitted that format's `x-api-key` header.
  Preserve the incomplete run at `/var/lib/aipg-backup/stream-disconnect-20260909/`.
  A corrected helper reran only Anthropic with a fresh temporary key; its proof
  and the three-job final audit live in the `-r2` directory. This was a canary
  client mistake, not evidence of a production auth regression. Both temporary
  keys were revoked and subsequent credit reads returned 401.
- Focused local admission, free-credit and promotion tests: 79 passed, with
  one existing websockets deprecation warning. SQLite/fake-store tests are not
  a claim of a live free-credit campaign or additional PostgreSQL race proof.
- The final process/config inspection corrects an earlier summary: daily-free
  spending is off, but the already-existing promotion gate is on for exact
  campaign `builder-2026-q3`. Welcome grants are not in that spendable list.
  Neither configuration changed in this pass; no new promotion was activated.
- Purchased balance: USD 9.772660. Cumulative test spend: USD 0.228015 of the
  approved USD 1. Global charging remains allowlisted; payout service/timer
  remain inactive. This does not close remaining frontend/identity, crash,
  canary-observation or payout-economics gates.

## Explicit admission and auth proof (2026-09-09, 17:07 UTC)

**Current state:** Core `ad5a1257`, Alembic `0040`, charging still allowlisted,
fixed prospective reward boundary unchanged, payouts still paused. This is a
verified-path containment step, not global charging activation.

| Public generation path | Current admission | Latest evidence / next gate |
| --- | --- | --- |
| Chat completions | Enabled | Paid first-party text/tool/image and direct-API receipts reconciled |
| Responses | Enabled | Paid normal and streaming terminals reconciled |
| Anthropic messages | Enabled | Paid normal and streaming Qwen3-27B terminals reconciled |
| Single text-to-image | Enabled | Paid Gallery/Chat images reconciled |
| Text-to-video | Enabled | Paid Gallery output and reload recovery verified |
| Image-to-video | Enabled | Paid multistage Director canary and second-segment recipe root verified |
| Audio | Enabled | Paid Music canary and reload recovery verified |
| Image-to-image | Disabled | API billing/recovery passed; visual editing QA remains outstanding |
| Image batches | Disabled | Four-output live attempt failed and fully refunded; worker deployment/retest required |
| Video timeline | Disabled | No successful paid timeline canary |
| 3D | Disabled | No online worker or paid canary |

- `GENERATION_ENABLED_PATHS` now explicitly selects the seven enabled rows
  above. It applies independently of charging mode to new work, including
  preview traffic. Existing result/history reads remain available. Ordinary
  chained Director segments use image-to-video; this does not enable the
  separate timeline recipe input. Do not infer every advertised model is
  priced or independently qualified from this per-path table.
- Activation at `2026-09-09T17:06:56Z` used the unchanged immutable release,
  both deployment locks, a fresh PostgreSQL backup/restore, temporary ingress
  gate and two quiet observations. Only the path-list environment key changed.
  All six supervisor/child environments match the seven paths and preserved
  account/model/service cohorts plus exact reward cutoff. Public ingress was
  restored, health reported healthy Redis and nine connected workers.
  Private evidence: `/var/lib/aipg-backup/verified-paths-20260909/`.
- A funded inference-only temporary key tested batch, timeline and img2img:
  all returned the explicit admission 503. 3D also returned 503, but its HTTP
  response was the earlier no-worker availability check; a separate check of
  the deployed admission function and environment proves 3D remains denied
  even if a worker connects. Do not label the HTTP response alone as that proof.
  Both Redis stream last-generated IDs, owner balance, reservation and credit
  row counts stayed unchanged. No work was dispatched and no charge occurred.
  Temporary key revoked; subsequent authenticated read returned 401.
- Before this change, all six base generation routes rejected missing keys
  with 401 and account-read-only keys with 403. The read-only key could read
  its funded account's credits, proving rejection was due to generation scope,
  not lack of funding. The twelve requests changed neither job stream nor
  owner economic rows. That key was also revoked and rejected afterward.
  Private evidence: `/var/lib/aipg-backup/auth-admission-20260909/proof.json`.
- Media-worker PR #25 merged as `b8324989df6a575a031a2c32d8e3e66799d3b310`.
  Authoritative `n` now overrides the adapter's legacy batch-size field; slot
  count must match before rendering and output count must match before upload
  or a signed success. Seven regressions failed before the runtime fix;
  191 local tests passed afterward, with two legacy-template tests skipped
  because their uncommitted Dreamshaper fixture is absent. Python 3.10/3.11/3.12,
  Linux/Windows manager builds, release gate and security CI passed. This code
  is merged, **not deployed to the live media worker**. Its actual host and
  version still need confirmation; the production batch root cause is not
  established by mocked tests. Never reopen batching on this merge alone.
- These checks spent zero credits. Last verified purchased balance remains
  USD 9.773036, cumulative canary spend USD 0.227639 of the approved USD 1.
  No new grant, treasury transfer, historical ledger rewrite or payout occurred.

Next gates: finish identity/failure and frontend QA, reconcile the configured
free/promo policy with tests (daily-free spending is off; the existing promo
gate is on for exact campaign `builder-2026-q3`, not welcome grants), satisfy the
runbook's canary observation/reconciliation requirement, and then explicitly
activate global charging on the verified list. Payout economics and historical
obligations are a separate review; paid-only eligibility is not payout approval.
The older snapshots and unchecked inventory below are chronological evidence,
not overrides of this current admission table.

## Passthrough and remaining image canaries (2026-09-09, 16:48 UTC)

Core remains immutable `ad5a1257` / Alembic `0040`, charging allowlisted,
with the exact `16:26:27Z` prospective reward boundary preserved. Payouts
remain inactive. The following live tests used the owner's existing purchased
credit and short-lived inference-only keys, revoked after each run.

| Path / mode | Model | Actual micro-USD | Refund micro-USD | Grid job |
| --- | --- | ---: | ---: | --- |
| Responses, non-streaming | GPT-OSS-120B | 4 | 74 | `808aa659-b34f-4c16-b94f-4efe4ce7ccbc` |
| Responses, streaming | GPT-OSS-120B | 6 | 72 | `01f9fc9b-5998-46b9-bb46-6148745087ec` |
| Anthropic, non-streaming | Qwen3-27B | 7 | 32 | `b744d908-1f58-416b-af98-77dea2eb5a58` |
| Anthropic, streaming | Qwen3-27B | 6 | 33 | `fb4b06a7-6bea-4a31-a04e-01a6d152d124` |
| Image-to-image | Krea 2 Turbo | 5,000 | 0 | `2a184d84-7d26-4288-960d-cb75641549fc` |
| Four-image batch, failed | Krea 2 Turbo | 0 | 20,000 | `0646f390-ff67-4388-9dd4-05e0e3cc17ef` |

- Both text formats returned HTTP 200, durable settled reservations, nonempty
  completion hashes, and fully purchased-backed reward eligibility. Streaming
  included actual text deltas and the native terminal (`response.completed`
  or `message_stop`). Non-streaming extraction also includes reasoning content;
  its truncated operator preview is not a claim that reasoning is visible UI.
  `all-paid-audit.json` reconciles all four jobs, credit refs and global/account
  balances without findings. Evidence: `/var/lib/aipg-backup/passthrough-canary-20260909/`.
- GPT-OSS-120B is not advertised for Anthropic: its live attempt returned 404
  before any reservation or charge. GPT-OSS-20B is advertised, but has no price
  entry. Its pricing preflight stopped before key issuance; a separate deliberate
  live rejection test then returned 402 (`has no text price`) without a
  reservation or balance change. Qwen38 Flash 125B also has no price entry.
  Neither is grandfathered into free paid-mode inference; pricing is unresolved.
- To exercise supported paid Anthropic traffic, the owner model restriction
  expanded in two supervised config-only changes: first GPT-OSS-20B, then the
  already-priced Qwen3-27B and DeepSeek V4 Flash NVFP4. No account, direct-service,
  promotion, free-credit, price, or reward-boundary setting changed. Both used
  environment snapshots, fresh backup/restore proofs, deployment locks, queue
  drains and temporary ingress gates. All six current processes match the
  nine-model cohort. Evidence: `/var/lib/aipg-backup/canary-model-20260909/`
  and its `-r2` sibling; second activation `16:42:10Z`.
- The existing empty direct-service account returned 402 for chat, Responses,
  Anthropic, image, video and audio. Both Redis stream last-generated IDs,
  reservation/credit-ledger counts and balance remained unchanged across all
  six requests. No generation was dispatched and the temporary key was revoked
  (subsequent read 401). `six-route-negative.json` records the zero-spend proof.
- Image-to-image used the owner's earlier Director first frame as input and
  requested a blue-to-red cube edit. It persisted one output and the reviewed
  img2img recipe root `0x238bcd412d1d677b38d9f512fec8158c9d2d71d4ca2ff9b93d3aee4aad57b6a6`.
  Authenticated result recovery returned the same output. This proves API,
  billing and recovery, not visual editing quality: browser QA was blocked by
  the locked Mac and remains pending. Evidence:
  `/var/lib/aipg-backup/image-billing-canary-20260909/img2img.json`.
- **Batch is NOT cleared:** the four-image request returned 502 because the
  worker-reported result count did not match four requested outputs. The initial
  canary observed the hold just before normal failure cleanup. The subsequent
  read-only `batch-failure-audit.json` proves a full 20,000 micro-USD refund,
  no worker completion/reward row, no stale hold, and no balance drift. A release
  uses terminal reservation status `settled` with null actual here; do not
  mistake that status alone for a paid success. No manual ledger edit or refund
  was made. Keep `image-batch` excluded from global paid admission until fixed
  and retested; this run is preserved as failure evidence, not a passed batch.
- Focused local passthrough/admission/media-contract verification: 77 tests
  passed with one existing websockets deprecation warning. Reward-cutoff docs
  PR #162 merged after its required PostgreSQL 16 CI and security checks passed.
- After the automatic batch refund, purchased balance is USD 9.773036;
  cumulative test spend is USD 0.227639 of the approved USD 1. Raw temporary
  hold deductions are not final spend. No new funding or worker payout occurred.

Remaining launch work includes visual/account failure QA, explicit global path
selection (unverified batch/3D/timeline must stay out), frontend rollout state,
and payout economics/reconciliation. Global billing is not yet enabled.

## Prospective reward boundary live (2026-09-09, 16:26 UTC)

- `WORKER_REWARDS_PAID_ONLY_SINCE=2026-09-09T16:26:27+00:00` is now loaded
  by all six Core supervisor/child processes on immutable `ad5a1257`.
  Preserve this exact boundary on rollback. Charging remains allowlisted;
  worker payout service and timer remain inactive. No funds moved.
- Before this timestamp, historical DEN eligibility is unchanged. At/after it,
  only the purchased fraction of settled credit-backed work qualifies for the
  unrestricted emissions pool. Free, promo-only, absent, held, released, and
  malformed reservations do not qualify. x402 still requires its separate
  settled-payment proof and remains dark.
- The deployed custodial allocator applies the prospective SmolLM-family cap
  across all accounts, including walletless accrual: at most 50 basis points
  of the period budget, preserving a smaller natural share and leaving clipped
  allocation unspent. This is not permission to send payouts or a full solution
  to low-traffic farming against the remaining fixed-budget model.
- Activation used both deployment locks, a fresh PostgreSQL backup/restore
  proof at Alembic `0040`, a bounded generation gate, and two quiet queue
  observations. A future boundary was chosen only after the drain. Only that
  environment setting changed; historical economic rows were not edited.
- The first post-restart operator check stopped because byte-exact hashes of
  floating-point aggregate results differed. The boundary and ingress gate were
  retained. The revised read-only proof checked per-record SQL eligibility
  exactly (zero changed historical rows) and compared every account/wallet
  allocation with identical attribution and a tight numerical tolerance.
  Maximum observed aggregate difference was `6.007030606269836e-08` DEN.
  The original operator script and failed check are retained, not relabeled
  as a successful run. No production accounting algorithm changed for this.
- The frozen pre-change window contains 124,407 completion rows, 14 account
  aggregates, and 11 wallet aggregates. Counts and historical totals match;
  comparison uses relative tolerance `1e-12` / absolute `1e-8` solely for
  floating-point sums. Per-record eligibility has no tolerance.
- Private evidence: `/var/lib/aipg-backup/reward-cutoff-20260909/`, including
  `policy.json`, original scripts, `verified-v2.json`, environment digest and
  backup/restore logs. Public ingress was restored after successful verification;
  health reports the exact release, Redis healthy, and nine connected workers.
- Local focused verification: 45 payout-allocation tests passed; reward tests
  passed 34 with 33 PostgreSQL cases skipped locally. The deployed commit's
  main CI `34372676936` passed with PostgreSQL 16 and the disposable reward-test
  database URL wired into the full suite. Do not present local skips as PG proof.
- The initial post-boundary read had zero completed jobs and zero unbacked
  eligible DEN. That verifies configuration, not a live workload outcome.
  A later `post-ingress-proof.json` at `16:29:36Z` observed seven real completed
  jobs with 106.3 raw DEN and zero eligible DEN; unbacked eligible exposure was
  zero. This proves exclusion on live unbilled traffic, not just an empty window.
- A bounded direct-API owner canary then returned HTTP 200 and the visible text
  `Billing verified.` Job `26e6049e-59f4-4743-9430-c3033463621e` reserved 78
  micro-USD, settled six purchased micro-USD and refunded 72. Its nonempty
  completion hash and 26.68 DEN were recorded, with all 26.68 remaining
  reward-eligible. The additional inference-only test key was revoked and
  rejected with HTTP 401 afterward. This did not send a worker payout.
  Private proof: `paid-canary.json`. Cumulative owner test spend is USD
  0.222616 of the approved USD 1; purchased balance is USD 9.778059.
  Remaining failure/modality canaries and payout economics still gate resumption.

Global user charging, remaining identity/failure and modality canaries,
explicit enabled-path selection, and payout economics/reconciliation remain
open. Do not clear the reward boundary to roll back an unrelated frontend issue.

## Direct services now charged (2026-09-09, 16:09 UTC)

- The current four direct-service principals are now selected in
  `GRID_CHARGING_ALL_MODEL_SERVICES`: `aigarth`, `aipg-music-local`,
  `codebase-design`, and `website-homepage-demo`. All retain their existing
  positive request/day caps. Missing funds reject; this is not a credit grant,
  a price change, or permission for unlimited sponsored inference.
- The configuration preview revalidated active direct keys, matching canonical
  service accounts and valid caps against read-only PostgreSQL. Exactly one
  environment setting changed under the deployment locks, with a pre-change
  digest check and preserved permissions/ownership. The owner/delegated-user
  cohort, model list, promotional settings, daily-free gate, and reward cutoff
  were not expanded. The core release remains `ad5a1257` / Alembic `0040`.
- The first attempt proved image/video 402 rejection, but audio returned 503
  during worker reconnection after restart. The guard rolled configuration back
  and retained the ingress gate for inspection. After confirming rollback and
  healthy workers, normal ingress was restored. The temporary test key was
  revoked. Preserve the failed attempt under
  `/var/lib/aipg-backup/direct-services-20260909/`; it is not passing proof.
- The second attempt added an explicit required-model readiness wait before
  issuing its short-lived canary key. It passed a fresh backup/restore proof
  and queue drain, then activated at `16:09:57Z`. All six inspected supervisor
  and child processes use the exact four-service cohort and allowlisted mode.
  Private evidence and operator scripts are retained under
  `/var/lib/aipg-backup/direct-services-20260909-r2/`.
- An additional two-minute, inference/account-read-only service key tested the
  existing zero-balance service through the live Core HTTP API on loopback
  while public generation ingress was gated. Text, image, video, and audio each
  returned 402. Neither Redis job stream's last-generated ID advanced; the
  service's reservation and credit-ledger counts and purchased balance remained
  unchanged. The key was revoked and a subsequent authenticated read returned
  401. Existing service keys were neither rotated nor revoked. Test spend: zero.
- Public ingress is restored and health reports healthy Redis and the exact
  unchanged Core release. Worker payout service and timer remain inactive.
  This check does not establish positive funding for the empty service, a
  paid service-generation canary, passthrough rejection coverage, or public
  all-account charging. New direct services must not be assumed charged merely
  because these four are in the cohort.

The prior direct-service exposure gate is closed for the current inventory.
At that observation, global user charging, the prospective paid-only reward boundary, remaining
identity/failure and modality canaries, and payout reconciliation remain open.

## Service-cohort hardening and Director evidence (2026-09-09)

- Core PR #158 merged as `275cda22275cc12e9983a033710abc875c72473d` after
  required PostgreSQL 16 CI, restore/schema parity, dependency audit, security
  scans, and the separate Core/Console/node integration passed. Its tree
  exactly matches tested head `9934f4c1`.
- A configured all-model direct service no longer drops out of charging when
  its request/day policy is missing or malformed. It reaches the fail-closed
  policy guard rather than returning a successful preview authorization.
  Global-off behavior and delegated user/model/account boundaries are unchanged.
- Production activated immutable `grid-core-275cda22` at `15:43:11Z`, after
  fresh backup/restore proof at `0040` and two drained-queue observations.
  Dependency lock, schema, migrations, environment, systemd and base Nginx
  config were unchanged. The temporary generation gate was removed, public
  health reports the exact commit, and an anonymous image submission returns
  401. All six inspected supervisor/child processes use the new release and
  preserved allowlisted charging configuration. Evidence:
  `/var/lib/aipg-backup/demand-release-275cda22/`; compatible rollback is
  `7cb79de8`. Payout service and timer remain inactive.
- This deploy does not add the remaining direct services to a billed cohort.
  Fresh read-only inventory found four direct-service keys with valid caps,
  but only the homepage demo in the all-model cohort. One of the other three
  has neither a purchased-credit row nor an active promotional grant. Do not
  invent purchased balance or grandfather unlimited free inference; enforce
  funded caps or reject before dispatch as the cohort is expanded.
- The first Director canary generated a paid first frame and a three-second
  segment, retained the signed-in session and result after browser reload,
  and played moving video. The first frame cost 5,000 micro-USD and the segment
  cost 60,000. Cumulative approved test spend reached USD 0.162610 within the
  USD 1 limit; account/global credit ledgers reconcile exactly.
- The unmodified audit correctly retained a `model_mismatch` finding: the
  reservation names the Director recipe and the ledger names its LTX-2.3
  checkpoint. Core's job-specific logs record a recipe content root matching
  the reviewed Director JSON and the required checkpoint route. However,
  image/video durable results were discarding the root (audio retained it).
  Keep the failed private `director-missing-recipe-root-audit.json`; historical
  economic rows are not rewritten. This is a routing-evidence defect, not a
  demonstrated billing loss or proof of worker model substitution.
- Console and Music retained login and refreshed to the same purchased balance
  after these canaries. Existing open pages can show a stale balance until
  refreshed; this check is not real-time cross-tab balance synchronization.
- Core PR #159 merged as `ad5a12572ab3539bd0e4ca3d4d396ee819b7197d` after
  required PostgreSQL 16 CI and security checks passed. The merged tree matches
  tested head `e51d372f`. The combined local suite passed 1,416 tests with 296
  external-service skips; those skips are not claimed as database proof.
- Production activated `grid-core-ad5a1257` at `15:52:09Z` after another fresh
  backup/restore proof at `0040` and queue drain. Public health reports the exact
  commit and healthy Redis; environment, admission cohorts, schema, dependency
  lock, and paused payout units remain unchanged. Protected evidence:
  `/var/lib/aipg-backup/demand-release-ad5a1257/`; compatible rollback is
  `275cda22`. Main CI is a separate follow-up from the completed PR checks.
- Image/video recovery now retains Core's dispatched recipe root. The canary
  audit accepts a differing checkpoint name only when the exact retained root
  resolves to the reviewed requested model, job type, and required checkpoint.
  It never relies on pricing aliases or a worker-supplied root. This is routing
  bookkeeping, not an execution-fidelity attestation. No historical row was
  backfilled, and the original Director audit remains a recorded failure.
- A second three-second segment used the first segment's last frame. Browser
  reload during rendering recovered the original pending job; it completed
  once, retained its Core receipt, and the six-second two-segment timeline
  played through. The read-only audit passed with exactly one 60,000-micro-USD
  debit, the matching durable Director recipe root, and zero account/global
  discrepancies, negative balances, invalid splits, or stale holds. Keep
  `director-chained-post-fix-audit.json` alongside the earlier failed report.
  Cumulative approved canary spend is USD 0.222610, within the USD 1 limit.
  This proves browser reload recovery, not a Gallery/Core process-crash test,
  video motion fidelity, export quality, or the remaining identity/failure cases.

Public charging activation, direct-service cohort expansion, prospective reward
eligibility, and payout resumption are still separate uncompleted gates.

## Funded owner canary and Chat output fix (2026-09-09)

Funding and a bounded test-spend approval have now been supplied. The prior
funding block below is historical, not the current blocker. Only the existing
owner account's charging-model cohort was expanded to `z-image-turbo`,
`gpt-oss-120b`, `krea 2 turbo`, `ltx-2.3`, `ltx director 2.0`, and
`ace-step-v1.5-xl-turbo`. This selects canary billing, not public launch readiness.
No other account or direct-service cohort was added; the existing homepage
all-model service policy was preserved.

- Chat PR #1 merged as `085e7394b50b2e72ee8a9181a073412490324918` and is live
  on the corrected `grid-085e7394b5-r2` images. Its first packaging attempt
  failed and was rolled back; Chat's production release guide records the
  asset-permission and additive-migration rollback fixes. The maintainer's
  exception covered unavailable inherited private-runner checks only, not
  AIPG tests or paid-canary gates. Chat PR #2 records deployment evidence.
- Real Chat text, Chat-generated Krea image and Music audio completed with
  account attribution and exact reserve/settle/refund reconciliation. Music's
  completed output reopened after reload without another generation. Chat's
  image and signed-in session also survived reload. This is not a process-crash
  proof or a cross-account authorization test.
- The initial Chat image workflow exposed a missing result hash on its
  tool-only text turn. The unchanged read-only canary audit returned failure.
  Historical ledger rows and charges were retained, not rewritten.
- Core PR #156 fixes the output commitment and tool-function metering and
  merged as `7cb79de8f6e2dd174b2ff345503e6ba69fd74a2b`. Required PR CI passed
  with PostgreSQL 16: 1,627 tests passed, 10 explicit skips, plus the separate
  Core/Console/node integration test. Dependency audit, restore/schema parity,
  full-history secrets/infrastructure scans and CodeQL passed. The merged tree
  exactly matched the tested PR tree, and merged-main CI also passed.
- Production selected immutable `grid-core-7cb79de8` only after a fresh backup
  restored/migrated on disposable scratch at `0040`. Locked dependencies,
  schema, migrations, systemd, Nginx and environment were unchanged. Exact
  generation routes were briefly gated while the queues drained; normal
  admission resumed after matching-commit health/Redis checks. `0d4545cd` is
  the compatible rollback; retained evidence is in
  `/var/lib/aipg-backup/demand-release-7cb79de8/`.
- The supervisor and five child processes were checked directly: exact new
  release, `allowlist`, one owner account, zero direct-service cohort entries,
  and the six approved models. Global charging, daily-free spending and the
  prospective reward cutoff were not enabled. Payout service/timer remain
  inactive; monitor and existing promotional settings were preserved.
- Repeating the Chat image workflow produced a distinct image and three
  successful jobs with result commitments. The read-only audit passed with
  exact debits/refunds, zero account/global balance discrepancies, no negative
  balances, invalid splits or stale holds. Keep both private
  `pre-fix-audit.json` (failed) and `post-fix-chat-audit.json` (passed) in the
  funded-canary evidence directory. The old failed job remains a known
  historical evidence gap; the passing report covers the new tested jobs.
- Gallery's single-image canary completed once and survived reload. Batch
  remains deliberately unavailable in the Gallery UI until the production
  workflow/cardinality proof exists.
- The funded LTX video completed with one 80,000-micro-USD debit and a valid
  result commitment, but reload exposed a Gallery read bug: three PostgreSQL
  readers discarded the stored video type and returned image. The object was
  a valid four-second H.264/AAC MP4; the original database row and durable
  receipt were correct. New paid generations were paused. Gallery PR #29 fixes
  single-item, private-history and favorite readers without data or schema
  changes. Its PostgreSQL regression failed in all three readers before the
  fix; local race tests and required PostgreSQL 16/browser/security CI passed.
  PR #29 merged as `02446c68`; its identical tested head `af7593fd` is live
  after backup/restore/race proof with existing rows and schema unchanged.
  Reloading the original canary restored video playback with visibly moving
  frames and the same receipt, without another generation or debit. This is
  not a video-quality, actual-dimension, in-flight crash or Director proof.
- At the post-Video audit, cumulative approved canary spend was USD 0.097610,
  within the USD 1 approval. All five post-Core-fix Chat/Gallery jobs reconciled
  with zero account/global discrepancies, invalid splits, negative balances
  or stale holds. Director and remaining failure, identity and service-path
  canaries are not signed off by these results.

Public activation remains HOLD. In particular, direct-service metadata ceilings
are not proof that preview traffic actually consumed exposure budget; the
remaining service cohorts need an enforced funding/disable policy. An unset
prospective reward cutoff must not be described as paid-only reward activation,
and the unrestricted pro-rata pool still needs its reviewed emission policy
before payouts resume. Do not remove those gates because the funded happy-path
canaries now work.

## Media recovery deployment (2026-09-09, 01:39 UTC)

- Core `4891565001ba18ca2bc4b4cfcdb818aeb7da5aeb` is selected at
  `/home/aipg/releases/grid-core-48915650`. This includes PR #149's durable
  media result/settlement and PR #150's private canonical ownership endpoint.
  Required PR and merged-main CI passed, including PostgreSQL 16 restore,
  schema parity, tests, and secret/security checks.
- A fresh `grid-postgres-20260909T013852Z.dump` was checksum-verified and
  restored into a generated scratch database. The exact candidate upgraded it
  from `0039` to `0040`; Alembic check found no new upgrade operations. The
  scratch database was dropped. Production remained `0039` during the proof.
- Only after that proof did production upgrade to `0040`, pass schema parity,
  and switch the immutable release. Production dependency lock, environment,
  Nginx and systemd definitions stayed unchanged. Payout service and timer
  remain inactive; the backup timer state was preserved.
- Read-only process inspection verifies the supervisor and children use the
  new release, `GRID_CHARGING_MODE=allowlist`, disabled daily-free spending,
  enabled billing/reward/treasury monitoring, disabled legacy probes, and the
  still-unset prospective reward cutoff and generation-path allowlist. The
  exact promotional campaign/cohort settings were preserved, not expanded.
- Public health returns the exact commit and healthy Redis. After reconnection,
  the connected-worker count returned from 8 to the pre-cutover 12, with the
  same model set. No error/traceback/handshake-timeout entries were found in the
  inspected post-cutover journal window. Both new private endpoints return 401
  without authentication. Those checks are not a funded-generation proof.
- Protected host evidence is in
  `/var/lib/aipg-backup/demand-release-48915650/`. Retain the compatible
  `c34c7da5` code rollback and the additive `0040` columns; do not downgrade or
  delete paid recovery records.
- Gallery PR #27 merged as `c23014761acc14f83030601b5f9bb71311ca6662`
  after PostgreSQL 16 backend, frontend, browser, full-history Gitleaks and
  Go/TypeScript CodeQL checks passed. It adds verified account-family recovery
  on top of PRs #25/#26. Gallery then activated that release at 01:49:57 UTC
  after its own restored-backup/full-Go-race proof and migration 0003. Existing
  rows and old migration checksums were preserved, the live journal started
  empty with RLS, and the existing Google session loaded private creations
  without re-login. Gallery PR #28 records the detailed host evidence. Paid
  multistage/restart and live wallet-merge canaries remain unproven.
- No funds moved, grants were minted, historical ledger entries edited, or
  charging/payout gates expanded. The last verified canary remainder is
  675 micro-USD. A funded streamed-text/audio/Director canary still requires
  additional user funding and spend approval; do not reuse the exhausted
  earlier USD 0.02 allowance as authorization for more spending.

## Reviewed deployment

- Core PR #132 initially deployed as `a754b6898b4a6111758b104ee633de18ef0fe252`.
  The coordinated worker-release task subsequently deployed combined commit
  `0b4630c555ac10be6a64b5610b493ca38c0730ac`, including service-budget PR #134.
  Independently re-read the selected production checkout to confirm that SHA.
  The deploying task reports fresh backup/restore at Alembic `0039`, unchanged
  environment/services, and all 14 pre-cutover worker connections recovered.
  Retained `a754b689` is the immediate rollback. This billing task must not
  perform a competing Core restart during worker qualification.
- Required CI passed with 1,399 tests passed and 9 skipped, PostgreSQL 16
  backup/restore proof, migration/schema parity, and secret/infrastructure scans.
  CodeQL also passed. Skipped cases and live canaries remain separate evidence.
- Candidate installed only the reviewed hash-locked binary wheels and passed
  `pip check`. No dependency lock, Alembic, systemd, or Nginx changes between
  the prior live release and this candidate; existing service/proxy assets stayed
  unchanged.
- Production backup restored into a disposable database using the exact new
  candidate: Alembic `0039`, no new upgrade operations, proof passed. This did
  not restore over or alter the live economic database.
- Configuration preflight and the restarted process confirmed charging still
  `allowlist`, one account, zero service cohort entries, `z-image-turbo` model
  cohort, and no `WORKER_REWARDS_PAID_ONLY_SINCE` value. The production environment
  file was byte-for-byte unchanged across cutover. The new economic policy is
  deployed but NOT activated.
- Verified the running process directory and both local/public health commit.
  Redis healthy; all 13 previously connected workers recovered after restart.
  Worker payout timer and service remain inactive; no payment or backpay ran.
- Music PR #3 merged and deployed as `b9713d996eeba87dee9ea1cbebb7948cebdc7141`.
  Container build/auth smoke and post-restart HTTP checks passed, with the
  signed-session delegation comparison active. Music PR #4 records that release.
  No billing flags or balances changed and no real audio generation ran.

## Verified containment

- Production `aipg-payout.timer` stopped and disabled; the payout one-shot was
  inactive with no in-flight process when paused. Rechecked inactive/disabled
  after the local implementation pass. Grid API remains active.
- Original environment preserved in the root-only rollout directory on the
  production host. Do not publish that file, credentials, or raw customer IDs.
- Fresh backup `grid-postgres-20260908T193201Z.dump` passed checksum verification
  and a full disposable-database restore with Alembic `0039` and no schema drift.
  Backup/restore used the deployed `84fe0fd6` release. Neither operation restored
  over the live database or changed historical payment records.
- Repaired only missing group access on the selected release root, `scripts/`,
  and the versioned backup script. The hardened backup service could not traverse
  their `0700` permissions. Future deployments must verify these exact paths.
- Last inspected runtime was `GRID_CHARGING_MODE=allowlist`, one account,
  `z-image-turbo` model cohort, and zero direct-service allowlist entries.
  Bounded all-model service exceptions require their own inventory; these counts
  alone do not prove that all other traffic is free or charged.

## Local implementation and evidence

Implementation branch: `fix/demand-billing-launch`, based on `61a9ffb9`.
The reviewed deployment above supersedes the initial local-only posture.

- Invalid explicit charging modes now reject startup before dependencies/loops
  and reject authorization, instead of silently falling back to free inference.
- Added real-Postgres text/media duplicate-terminal and terminal-versus-refund
  races. Only a committed terminal may leave both a worker ledger entry and a
  charge; refund-winning races cannot leave a payable completion.
- Added prospective purchased-fraction reward aggregation for account, wallet,
  and diagnostic consumers. Legacy rows remain unchanged. New unreserved,
  free-only, promo-only, held, refunded, malformed, and unknown-source work is
  ineligible for the unrestricted pool. x402 retains its external-settlement gate.
- Added a network-wide 0.5% SmolLM-family custodial emission cap with no
  redistribution, including account splitting and walletless accrual tests.
- Full Core suite before the reward-policy additions: 1,124 passed, 230 skipped.
  The configured credit and payout concurrency modules ran against isolated
  PostgreSQL 14.19. Other skipped external integrations are NOT verified.
- Reward eligibility, cap, and x402 focused suite: 71 passed, including both
  SQLite with foreign keys and real PostgreSQL reward queries. Full candidate
  suite after final UUID lookup compatibility checks: 1,178 passed, 230 skipped.
  Matching production-version PostgreSQL CI and live canaries still required.
- Reservation correlation uses indexed primary-key comparisons for canonical
  and compact UUID spellings, not per-row functions on the reservation key.
- Source review: Music and Gallery send delegated user tokens for generation
  and preflight quotes. Gallery checks the refreshed identity against its signed
  session. Music lacked that session comparison; a separate candidate branch
  `fix/music-billing-identity` added it with no-dispatch/no-credit-read tests and
  has now deployed. Gallery's currently deployed flow still needs live verification.

## Service and Chat image follow-up

- A fresh read-only production inventory found nine active service clients,
  including four keys with direct-inference permission. Every current direct
  service has positive per-request and daily caps. The uncapped MCP bridge has
  no direct-inference permission; its delegated users still require their own
  credits when charging is selected. No keys, caps, or balances were changed.
- Core PR #134 merged as `c40232b2`: runtime service-budget validation now
  rejects missing/malformed direct-service caps rather than relying only on
  provisioning checks. Zero no longer means unlimited in the Redis guard.
  Configured delegated-app caps are validated, but omitted app-wide ceilings
  remain compatible with user-owned charging. It is now included in the
  combined `0b4630c5` production release above; no spending caps changed.
- Focused Core auth/billing/router checks: 377 passed, 41 skipped. Required
  PR CI also passed with PostgreSQL 16 restore/schema and money-path checks;
  skipped and mocked cases are not production canary evidence.
- Chat's running image-tool, constructor, and identity-module hashes match
  the reviewed pre-fix source. Text uses delegated identity; the image tool
  does not. Its current database provider uses an older internal endpoint and
  a different key from the canonical Chat service. A read-only Core credits
  request with that image key succeeds with `charging_enabled=false` and
  `service_budget=null`. Do not treat Chat text wiring as image billing proof.
- Chat PR #1 (`fix/chat-image-billing`) adds a per-image lazy user-token factory
  and requires the canonical Chat service endpoint/key for that delegation.
  Local identity/model-sync/image-tool/provider checks: 44 passed, with Ruff
  and targeted type checks passed. PR review/CI, provider configuration migration,
  deployment, and a real user-attributed image canary are still required.
  No production image-provider record or credential was changed during audit.
- Chat's inherited Python CI requested unavailable upstream private runners.
  A fork-specific hosted-runner adjustment preserves the full test commands and
  hash-verified dependency install. On commit `dab173b24d`, unit CI ran:
  4,989 passed, 30 skipped. Full type CI exposed fixture-type errors; corrections
  are pushed in `3877c22c38` with 49 focused tests and full local type checking
  passed. Subsequent hosted unit and full type CI passed; commit `d490442316`
  also passed the real PostgreSQL migration suite (15 passed, 1 skipped).
  Other inherited infrastructure-dependent workflows
  remain unverified; none of these results proves a live Chat deployment.

## Signed-in Gallery canary

- The user signed in normally. No synthetic session, credit grant, funding
  transfer, or charging configuration change was used for this canary.
- Krea remained preview-only for this cohort. Selecting Z-Image Turbo changed
  the UI to an estimated paid charge of USD 0.003. The pre-canary displayed
  purchased balance was USD 0.0037 (rounded).
- Generated one private 1024-square image on 2026-09-08 at 20:57 UTC, Grid job
  `8f01d616-01d1-4d5e-b6f0-d9797f54a214`, through the real Gallery UI.
  The UI returned the image with an 8.9-second generation time.
- Read-only production SQL confirms one settled `aipg-art` reservation:
  reserved/actual 3,000 micro-USD, zero free/promo allocation, source `credits`.
  Exactly one matching-account credit entry is -3,000 (`reserve:image`), and
  exactly one completion records one image and 0.32 raw den. No refund is due
  for this exact-priced success. Remaining purchased balance is 675 micro-USD.
- A second UI submission returned insufficient Grid credits and no new image.
  This verifies the visible rejection, not by itself a full queue-level
  no-dispatch proof. Automated backend rejection tests cover that boundary.
- Gallery PR #20 merged as `5836668b91a25cb2a1491af1fca6d8b1fa6d1cb6`
  after all backend/frontend/browser/security/CodeQL checks passed. It also
  verifies that Core credit/quote response IDs match the delegated account
  before generation. That release is now active after the production Next build,
  production-only lockfile reinstall, Go race tests/vet/build, and health checks.
  Both services are active; the running backend binary hash matches the candidate.
  Environment, Nginx, and service definitions stayed unchanged, with `2bc9c7e6`
  retained for rollback. A fresh signed-in Google page load retained the session,
  loaded the stored canary image, and showed the same paid balance and Z-Image quote.
- This proves one purchased-credit Gallery image path only. Cross-site balance
  parity, other modalities, refund/crash canaries, global charging, and prospective
  worker reward activation remain incomplete. Raw den is not evidence that the
  still-unset purchased-only reward cutoff has been activated.

## Admission candidate and coordinated release state

- Core runtime advanced to `d139835324fe9b05820b1ada1984fea0bb9fb538`
  through the coordinated worker-setup release. The subsequent `976db957`
  main commit is documentation-only. Do not deploy an older billing checkout
  over those fixes.
- PR #138 includes the billing evidence previously proposed in PR #135 and
  adds `GENERATION_ENABLED_PATHS`, an explicit public generation allowlist.
  Omission preserves existing paths for a compatible dark deployment; an
  empty JSON array rejects all new public generation. This is independent of
  charging cohorts and has not been activated in production.
- Text, passthrough, and shared media admission reject disabled paths before
  reservation/dispatch. Image batches, image-to-image, image-to-video, and
  video timelines require both their base modality and extra capability.
  The Chat media shim uses the same media admission check. Listings and quotes
  are not admission guarantees. Existing jobs retain their terminal processing.
- Local verification: 44 focused admission tests passed. The full Grid suite
  before the final media-shim regression passed 1,225 with 264 skips. Required
  PostgreSQL 16 CI on the final combined candidate remains a merge gate;
  local tests and older-head CI are not substitutes.
- Worker setup staging also recorded two ordinary fixture jobs (4.58 raw den,
  no reservations). The fixture was stopped, its key revoked, and its payout
  address cleared after confirming no payout records. Preserve those rows and
  use the protected staging audit for explicit historical payout exclusion:
  removing the wallet does not remove denominator weight, and a prospective
  reward cutoff cannot retroactively exclude these jobs.

## Combined admission and monitor deployment

- PR #138 merged as `91e19739`; PR #139 then merged as
  `adc9a21ec304fb7c61c536c4417711f284c6fc60`. The latter is now the
  immutable production release (cutover verified 2026-09-08 at 21:50:39 UTC),
  retaining the worker setup fixes from `d1398353`.
- Required PR #139 CI passed on PostgreSQL 16: 1,486 Grid tests passed with
  9 skips, backup/restore and migration parity passed, and the real
  Core/Console/node handoff passed. CodeQL and secret/infra scans passed.
  Local focused verification additionally passed 41 billing/alert tests,
  including real isolated PostgreSQL 14 concurrency; that scratch server was
  stopped after testing.
- The monitor now reconciles each purchased-credit account in one SQL
  statement. Equal-and-opposite account discrepancies no longer cancel out,
  and concurrent commits cannot split its reads across snapshots. Production
  read-only preflight found six accounts, zero mismatches, zero negative
  balances, and zero net drift. The equivalent complete query took 0.422 ms
  at current size; future ledger growth still needs operational monitoring.
- Before cutover, the candidate installed the unchanged hash-locked binary
  dependencies and passed `pip check`. A fresh 119 MB Grid-schema backup was
  restored into a generated scratch database and checked against the candidate.
  Live Alembic parity remained `0039`; no production migration was needed.
  Protected evidence is under
  `/var/lib/aipg-backup/demand-release-adc9a21e/`, including the snapshot,
  checksum, restore/drift proof, configuration backup, and timer comparisons.
- Public health reports the exact new SHA and Redis healthy. The supervisor
  and child processes use the new immutable working directory; billing source,
  admission source, and config hashes match the candidate. The API is active
  with zero automatic restarts, and anonymous assignments still return 401.
  Worker reconnection was checked separately from API health: all 12 workers
  online before restart returned. The last, serving `qwen3:1.7b`, reconnected at
  21:54:10 UTC; public health then showed the complete pre-restart model list.
- Configuration, Nginx, unit definitions, and timer states were preserved.
  Process environment confirms `GRID_CHARGING_MODE=allowlist`, legacy charging
  flag 0, daily-free spending 0, promo global gate 1, and both the generation
  admission allowlist and prospective reward cutoff unset. Promo spending
  remains subject to its separate campaign allowlist; the global gate alone
  does not prove a funded grant. One static, non-economic rollout test alert
  was queued and accepted by the configured Discord transport using the deployed
  alert module. This proves HTTP delivery, not that a human saw it or that every
  billing fault triggers the right alert. Payout timer and sender remain inactive.
- Rollback target is `grid-core-d1398353`, with the compatible `0039` schema
  retained. This was a code deployment, not global billing activation: new
  admission restrictions and the reward cutoff still require deliberate setup.
- Chat packaging at `ced82ded459f646d1742374196599403e4510361` passed hosted
  Linux/amd64 Dockerfile build, packaged-source hash comparison, and offline
  imports as UID 1001 (run `34281434136`). That image is CI build evidence,
  not a published production artifact or a Chat deployment. Its other inherited
  infrastructure-dependent checks remain unverified.

## Read-only reward-policy simulation

- A standalone read-only process applied the proposed paid-only rule in memory
  to the completed 2026-09-08 20:00-21:00 UTC window. No setting, reservation,
  credit, payout, or historical ledger record was modified. This is a
  counterfactual, not a revised allocation or authorization to pay that hour.
- The unchanged policy selected 3,822.43 raw DEN across six accounts. The
  simulated paid-only rule selected 0.32 DEN for one account, matching the
  purchased Gallery canary. This is evidence that the eligibility filter
  excludes the unbilled work in this sample, not a complete fraud-detection proof.
- The current runtime hourly budget is 208.33 AIPG. The allocator still awards
  the whole budget when one non-SmolLM job is eligible: the USD 0.003 image would
  receive 208.33 AIPG under this simulation. No token/USD valuation is implied.
  The SmolLM-family cap does not limit other models' low-demand windfalls.
- Payout resumption therefore needs an explicit decision about subsidy
  intensity, not just a billing flag: either deliberately approve a fixed
  bootstrap pool at low demand or introduce a reviewed per-work/revenue-linked
  emission ceiling with clipped allocation left unspent. Do not invent a
  token valuation or silently introduce that economic policy during deployment.
  Keep the payout timer paused until this and historical exclusions are reviewed.

## Monitoring follow-up (not production activation)

- Treasury monitoring merged through PR 141 as
  `1242ab8b118c9fe5f66b0a207477ff8f1ad76ea6`. Required hosted tests passed:
  1,511 Grid tests with 9 skips, PostgreSQL 16 restore/schema proof, 15
  anti-gaming tests, and the Core/Console/node handoff. CodeQL and secret gates
  also passed. The running Core remains `adc9a21e`; this merge did not deploy
  or configure treasury warnings.
- The reward-backing monitor is a separate default-off candidate. It observes
  the last complete UTC hour using the allocator's current prospective boundary
  and shared purchased fraction. It includes walletless accrual and warns about
  legacy unbacked eligibility, including mixed free/promotional shares, without
  claiming a transfer occurred. Unfunded x402 and work already excluded by the
  prospective rule do not trigger false reward-exposure alerts.
- Candidate verification: 72 focused tests passed across SQLite and an isolated
  local PostgreSQL 14 cluster, including unchanged historical DEN and boundary
  behavior. The scratch cluster was stopped afterward; production was not used
  for these tests. Full local Grid suite: 1,270 passed, 277 environment-dependent
  skips. Hosted PostgreSQL 16 CI and production query-performance/alert proofs
  remain required. A clean recent-hour report does not reconcile historical
  backpay or fix the low-demand fixed-pool economics described above.

## Monitor release deployed, activation still separate

- PR 142 merged as `4fa8bb651ed6e7f04c6adcdeca97ca81c91391c7` with
  a byte-identical tree to its tested head. Required run `34285211188` passed
  1,538 Grid tests with 9 skips, 15 anti-gaming tests, the PostgreSQL 16
  backup/restore/schema gates, and the real Core/Console/node handoff.
  CodeQL and secret gates passed.
- A standalone read-only production candidate query for 2026-09-08
  21:00-22:00 UTC completed in 0.0808 seconds, reporting 263 jobs and
  3,625.38 DEN without full purchased backing under the existing unset cutoff.
  This is unrestricted-pool eligibility exposure, not evidence of transfers.
  The process used read-only transactions and a 15-second statement timeout.
- The treasury candidate read both balances at one Base block against the
  configured payout signer address and token; token decimals were verified as
  18. Gas balance was approximately 0.00723 ETH and AIPG balance approximately
  0.00284. Hypothetical thresholds of 0.001 ETH and 5,000 AIPG correctly
  classified only AIPG as low. No threshold was persisted and no refill occurred.
- Immutable release `grid-core-4fa8bb65` became current at
  2026-09-08T22:27:04Z. A fresh protected database backup was restored and
  tested against the candidate before cutover. Evidence is under
  `/var/lib/aipg-backup/demand-release-4fa8bb65/`. Live Alembic remains `0039`;
  no production migration was needed. Rollback is `grid-core-adc9a21e` with
  that compatible schema retained.
- Public health and supervisor/child working directories report the exact
  release. API is active with zero supervisor restarts. Config, Nginx, systemd
  units, and timer state were preserved. Payout timer and sender are inactive.
  Charging remains allowlisted, admission restrictions and the prospective
  reward cutoff remain unset, and both new monitor flags remain unset/off.
  This deployment is not global charging or monitor activation.
- Post-deploy read-only reconciliation found six purchased-credit accounts,
  zero mismatches, zero negative balances, and zero aggregate drift. Equivalent
  health SQL executed in 0.302 ms at the current small dataset size; this is
  not a future-scale performance guarantee.
- All 12 pre-restart workers and 16 advertised models were online again by
  2026-09-08T22:28:55Z, including the delayed Qwen 8B reconnect. This is fleet
  recovery evidence, not a paid generation canary for each model.

## Read-only monitors activated

- At 2026-09-08T22:33:37Z, enabled `GRID_REWARD_MONITOR_ENABLED` and
  `GRID_TREASURY_MONITOR_ENABLED` on the same immutable `4fa8bb65` release.
  Treasury warnings use the verified payout signer address and token with
  thresholds of 0.001 ETH and 5,000 AIPG (18-decimal raw units).
- The preflight validated the candidate settings before activation. Exactly
  six monitoring keys changed; all other parsed environment values were
  identical. The protected snapshot and activation proof are in
  `/var/lib/aipg-backup/monitor-activation-4fa8bb65/`. Supervisor and child
  environments subsequently showed both flags set to `1`, with charging still
  `allowlist` and the reward cutoff still unset.
- A standalone invocation of the deployed checks, using read-only SQL and the
  production RPC, exercised both expected warnings. Discord transport accepted
  `treasury_low_aipg` and `unbacked_reward_eligibility`; the two extra events were
  marked `delivery_canary` and used separate dedupe keys. This proves the real
  check-to-transport path, not human receipt or a completed operator response.
  Normal background warnings retain the existing cross-process dedupe window.
- Core remained healthy and all 12 workers recovered. No credit, reservation,
  historical DEN, reward policy, or payout state was changed by activation.
  Both the payout timer and sender remain inactive.
- To roll back only these warnings, disable their two flags. Never restore
  the entire historical environment snapshot after subsequent billing/reward
  configuration changes: doing so could clear a newly activated cutoff or
  reopen generation. Preserve the economic settings and immutable release.

## Additional dispatch gap: legacy coordinator sampler

- Runtime inspection of `4fa8bb65` found `GRID_PROBE_ENABLED=1`, interval
  300 seconds, and a 256-token cap in the actual supervisor/child environment.
  The old `services/probe.py` submitted ordinary text jobs without reservation
  or an assignment-bound no-DEN terminal. Its evidence-only verdict framing
  did not prevent successful jobs from producing ordinary worker DEN.
- The retirement candidate removes the startup task, replaces old callable
  entry points with a fail-closed compatibility shim, and removes enabling
  instructions from the environment template. It leaves registered-validator
  assignments, manager setup canaries, and the separately budgeted audit rail
  unchanged. The updated probe document separates verdict authority from
  execution economics.
- A regression test first reproduced ordinary queue submission and now proves
  no submission even with the legacy flag set. Full local Grid suite: 1,271
  passed, 277 environment-dependent skips. Required hosted CI and deployment
  remain pending for this candidate; do not describe the production gap as
  closed merely because the source fix exists.
- At deployment, set the legacy enable flag to `0` while preserving all other
  settings, so rollback to the previous code cannot restart that sampler.
  Keep payouts paused and preserve historical ledger rows. This finding does
  not establish which historical payments came from these probes or attribute
  all unbilled traffic to abusive operators; reconciliation is separate.

## Legacy sampler retirement deployed

- PR #144 merged as `c34c7da511b640ef776e4a5c085d8a765c1cb12b`, with
  an identical tree to tested head `dfef9e49`. Required hosted run
  `34287466504` passed 1,539 Grid tests with 9 skips, 15 anti-gaming tests,
  PostgreSQL 16 backup/restore and schema parity, and the real
  Core/Console/node handoff. CodeQL and secret/infra scans passed.
- Immutable `grid-core-c34c7da5` became current at
  `2026-09-08T22:55:11Z`. Before cutover, the unchanged hash-locked dependency
  installation passed `pip check`. Fresh backup
  `grid-postgres-20260908T225308Z.dump` passed checksum and disposable restore
  proof against this candidate. Live schema parity remains Alembic `0039`;
  no production migration ran. Protected evidence and configuration snapshots
  are under `/var/lib/aipg-backup/demand-release-c34c7da5/`.
- The only parsed configuration change was `GRID_PROBE_ENABLED=0`.
  Supervisor and child processes use the exact new release and flag value;
  the new source does not start the sampler even if the old flag is restored.
  Registered-validator assignments and manager setup canaries were not changed.
  Nginx, service units, and timer states were preserved. API is active with
  zero automatic restarts, and both payout timer and sender remain inactive.
- All 12 pre-restart workers and 16 model identities were online again by
  `2026-09-08T22:56:16Z`, including the delayed Qwen 4B reconnect. Public
  validator status remained available with seven fresh heartbeats. These are
  recovery observations, not proof of new post-restart paid jobs or independent
  validator qualification.
- Post-cutover read-only reconciliation found six purchased-credit accounts,
  zero mismatches, zero negative balances, and zero net drift. No historical
  DEN, credit, or payout row was rewritten. Treasury/reward monitoring remains
  enabled; charging is still allowlisted, with the generation allowlist and
  prospective reward cutoff unset. This closes the old sampler's dispatch
  bypass, not all public billing or historical reward reconciliation.
- Code rollback is `grid-core-4fa8bb65` with schema `0039` retained and
  `GRID_PROBE_ENABLED=0` kept. Do not restore the pre-retirement environment:
  that would restart the unsafe sampler on the old code. Preserve any later
  economic configuration changes as well.

## Music durable recovery deployed - 2026-09-09

- Music PR #6 merged as `125141cbea1e93732cd0622ab0105eb3266611a0`
  after passing PR and exact-main CI. The immutable `music-125141cb` release
  is deployed; its source archive checksum was verified on the app host.
  Frozen installation, lint, types, production build, built-app auth/journal
  smoke and dependency audit passed there under Node `24.18.0`.
- The private SQLite request journal now lives outside release directories.
  Explicit one-time initialization, service ownership and restrictive modes
  were verified. Runtime uses that path and the exact reviewed release;
  service/session credentials and the original environment file are unchanged.
  Live journal and initial backup each passed integrity checks with zero rows.
- Generation admission was briefly closed for activation, with no established
  app connections observed before restart, then restored after HTTP checks.
  The proxy now rate-limits authenticated receipt recovery. Anonymous generation
  and recovery returned `401`; a bounded recovery burst also produced `429`.
  No production generation, credit grant or payment was performed.
- The implementation commits a local request claim before the one allowed Core
  submission, and recovers the owner's original Core result without resubmitting.
  Cross-process races and kill/restart were tested against local HTTP stand-ins,
  not a paid production job. Preserve the journal and WAL across rollback;
  keep admission closed on a pre-journal release.
- Signed-in Music account/balance parity, funded audio, actual debit/result
  reconciliation, and recovery during a real job/restart remain open. Browser
  access was locked during deployment. Neither deployment nor anonymous HTTP
  checks close these gates. Core charging remains allowlisted, global charging
  and spendable daily-free credit are off, and both payout units are inactive.
- Detailed evidence and rollback boundaries are recorded in
  [Music's journal release record](https://github.com/AIPowerGrid/aipg-music/blob/2558ef3cf9d852d4f0c1258f2b8d4c73c48b167a/deploy/JOURNAL_RELEASE_2026_09_09.md).

## Payout input hardening staged - 2026-09-09

- Reviewed main `e1c75d7e091468267ca40ad4e483b3c66edfda94` (PR #152)
  passed exact-main test run `34310152427`, including PostgreSQL backup/restore
  and the Core/Console/node handoff. Secret and CodeQL checks also passed.
  Its only runtime-source change since deployed `48915650` rejects malformed
  payout inputs before producing any payable or accrued allocations. It does
  not alter the reward rate, prospective cutoff or historical obligations.
- Prepared `/home/aipg/releases/grid-core-e1c75d7e` from the exact Git commit.
  The detached checkout is clean; production lockfile, schema and migration
  sources match the live release. Fresh hash-locked binary-wheel installation
  and `pip check` passed. This release is staged, **not selected or running**.
- Fresh backup `grid-postgres-20260909T045746Z.dump` passed checksum,
  scratch restore and candidate Alembic parity at `0040`, completed at
  `2026-09-09T04:58:23Z`. The generated scratch database was dropped; an
  independent query found no remaining restore-proof databases.
- Protected proof and configuration snapshot are under
  `/var/lib/aipg-backup/demand-release-e1c75d7e/`. The environment checksum and
  service/timer state, including the live API PID, matched before and after.
  Production remains `48915650` at `0040`; both payout units remain inactive.
  No source configuration, live schema, account balance or ledger was changed.
- Candidate preparation does not authorize a payout or resolve the earned
  reward ceiling. Revalidate current configuration and backup freshness before
  cutover; preserve stopped payout units until policy reconciliation passes.

## Payout and admission guards deployed - 2026-09-09

- Core `0d4545cda406bdd67181b55ab733b4ce177675c0` is selected through
  the immutable `grid-core-0d4545cd` release, activated at
  `2026-09-09T05:20:19Z`. It includes PR #152 payout-input validation and
  PR #154's explicit-path requirement for global charging. It supersedes the
  staged-only payout candidate above; that candidate was never selected.
- PR #154 and exact-main run `34313977617` passed, including PostgreSQL
  backup/restore/schema checks, the full Grid suite and the Core/Console/node
  handoff. CodeQL and secret/infra checks passed. The merged tree matches the
  reviewed PR head. Local suite: 1,326 passed and 296 environment-dependent skips.
- Fresh backup `grid-postgres-20260909T051533Z.dump` passed checksum,
  disposable restore and schema parity at `0040` before activation. The
  scratch database was removed. Production schema and locked dependencies
  are unchanged; no live migration ran.
- New public generation routes were briefly rejected at the proxy. Two drain
  checks found zero pending jobs and no actual undelivered stream entries.
  Redis's text lag counter alone was stale after deletions; it was not used
  as proof that nine jobs existed or as a reason to mutate the stream.
  The release switched only after draining; the temporary gate was removed
  following health/configuration checks and successful Nginx validation.
- Supervisor and child-process working directories identify the exact release.
  Running configuration remains `allowlist`, legacy charging flag `0`, daily
  free spending `0`, and the prospective reward cutoff unset. Promotional
  spending remains subject to its existing campaign policy. Reward/treasury
  monitoring stays enabled, and the legacy sampler stays disabled. The
  environment checksum and payout/backup timer state are unchanged; both payout
  units remain inactive. Public health reports the new commit and anonymous
  generation returns `401` after admission restoration.
- All 12 pre-cutover workers reconnected and the prior model set is preserved.
  Read-only, repeatable-read purchased-balance reconciliation checked six
  accounts: zero mismatches, negative balances or net drift. This is current
  accounting consistency, not proof of a new paid inference or reward policy.
- Protected evidence is under
  `/var/lib/aipg-backup/demand-release-0d4545cd/`, including configuration,
  backup/restore, drain, cutover and process proofs. No generation, credit grant,
  funding, payout or historical ledger rewrite was performed by this deployment.
- Code rollback selects `grid-core-48915650` with schema `0040` retained.
  Preserve current economic settings and stopped payouts; do not restore a
  stale environment snapshot or enable global charging on a release without
  the explicit-admission guard. Prefer a compatible forward fix.
- This is a dark safety deployment, not completion of signed-in account/balance,
  funded multimodal, Director, refund/recovery or reward-policy canaries.

## Remaining launch checklist

- [x] Review and merge candidate; record exact release SHA and CI evidence.
- [x] Select and verify the immutable prospective cutoff; preserve it on rollback.
- [x] Review payout query performance at current production load; see 21:56 UTC
      EXPLAIN evidence. This is not a large-fleet load qualification.
- [ ] Reconcile requested emission budget, no overlapping payout periods, and
      every payout entrypoint before restarting any sender. No treasury refill.
- [x] Verify credits and free/promo ceilings in actual production processes;
      retain the exact builder campaign, not a blanket promotion enablement.
- [ ] Complete request-to-terminal inventory below, including negative tests.
- [ ] Prove Google-only, wallet-linked, zero-balance, free-exhausted, and capped
      direct-service cases; same canonical balance across first-party apps.
- [x] Run funded success canaries on the seven enabled paths, including
      streaming disconnects and multistage Director; reconcile receipts.
- [x] Prove actual worker-handler failures/timeouts on PostgreSQL for all six
      formats, and Redis retry/duplicate/process-kill boundaries; PR166/167.
      These are controlled component tests, not a live GPU outage experiment.
- [x] Prove Core SIGKILL/restart against actual PostgreSQL/Redis with HTTP and
      worker WebSockets; 27 cases with synthetic worker output/storage.
- [ ] Complete the runbook's 24-hour allowlist observation before global
      expansion. Local accelerated-clock tests do not satisfy this window.
- [x] Retry the real funding receipt twice without another transfer or credit.
- [x] Verify funding retry persists receipt without another transfer (required
      Console browser fixtures, deployed UI and separate live duplicate claim).
- [ ] Verify billing/orphan/reward/treasury alerts and operational response.
- [x] Deploy reviewed Core/Music code with existing configuration preserved.
- [ ] Verify remaining frontend releases and final activation configuration.
- [x] Migrate Chat image provider to canonical service credentials only with
      the delegated-image release; verify the charged account and retain a
      protected rollback record. Fresh running-source/provider check passed.
- [ ] Inventory old-key consumers before revocation.
- [x] Restrict admission to the seven verified public paths; preserve the list
      on rollback. Global charging remains a separate activation gate.
- [ ] Resume payouts only after prospective policy reconciliation passes.

## Entry-point inventory

Source wiring is not live proof. Each row still needs exact account/service
attribution, reserve-before-dispatch, terminal/refund, and rejection evidence.

### Media and Director source trace

This trace uses Core `c34c7da5` and Gallery `5836668b`, the deployed runtime
sources, rather than the older local Gallery main checkout.

- Core image, video, audio, and 3D routers authenticate with `inference.submit`
  and pass the resolved billing user to `services/media.submit_and_wait`.
  That shared path validates admission/batch support, calls
  `authorize_media(record_reservation=True)`, and only then submits a queue job.
  This is charge enforcement for selected live cohorts, not a claim that
  allowlist-excluded traffic is already charged.
- Gallery `handleCreateJob` checks its signed-session account against Core's
  service identity exchange, checks the quote account, and sends the same
  delegated token in `GenerateMedia`. Quotes do not reserve funds; Core still
  owns the actual atomic authorization after this preflight.
- Studio batches use one `createJob` call with `n=4`, not four independent
  requests. Core reserves the full batch price under one job ID. The media
  terminal requires every unique presigned output slot before atomic
  completion/settlement. Incomplete batches release the hold instead of
  settling the full batch; `test_media_output_contract.py` covers that boundary.
  Native source-image/video batching is rejected before billing by
  `test_media_contract.py`. A live paid four-output batch is still outstanding.
- Director generates its optional Krea first frame and each video segment
  through separate `createJob` calls. Each therefore has a separate Core
  reservation and receipt. A recipe-unavailable fallback is another request,
  not reuse of an existing hold. Media errors before queue submission release
  their hold; after submission the worker terminal/reclaim/sweeper remains
  authoritative, not the HTTP timeout.
- Found a client retry gap: reload recovery turned untracked in-flight
  segments into idle work, allowing Render pending to submit replacements;
  an older output could also falsely mark a rerender done. Gallery PR #22
  preserves identifiers and keeps uncertain work out of the automatic queue.
  Five new regressions cover uncertainty and tracked-job continuity; all
  98 frontend tests pass locally on Node 22.23.2. This safeguard was deployed
  in Gallery `815c11ee` at 23:24:42 UTC (release evidence below).
  It is not durable request idempotency:
  Gallery pending jobs are in memory, and a lost HTTP response or process
  restart still requires outcome recovery before a safe retry. Keep paid
  Director launch unverified until this lifecycle and a live multistage
  canary are proven; do not treat the client safeguard as closing that gate.

### Gallery patched release: 23:24 UTC

- Gallery PR #22 merged as `815c11eef601486d83a9c216f63f620bb56d359e`.
  Its tree matches tested head `1ef05d2e`. All CI checks passed in run
  `34289126286`; both CodeQL language jobs passed in `34289126269`.
- Alongside the Director recovery safeguard, this release pins Next.js and
  its ESLint config to `16.3.4` and sharp to `0.35.4`. The production dependency
  audit passes the high/critical gate with two moderate and one low finding
  remaining. This is not a claim of a vulnerability-free dependency tree.
- The LXC candidate passed the Node 22 production build and production-only
  frozen reinstall, Go race tests, vet, and binary build. The first helper
  stopped after a successful web build because sharp does not export
  `package.json`; verification was corrected to `require("sharp").versions.sharp`
  and resumed against the existing candidate, not an assumed successful build.
- The candidate web process returned 200 for `/create` and `/create/director`
  on a loopback-only alternate port before activation. That process was stopped.
- New job and prompt-enhancement submissions were temporarily gated at Nginx.
  Status polling and browsing remained available. After a 45-second drain,
  the current Go invocation's journal showed zero generation starts and zero
  terminals across 31 entries. This is journal-derived evidence, not a durable
  pending-job registry. The first gate check raced Nginx reload; the helper
  stopped before switching releases, then resumed only after the live 503 gate
  and original configuration backup were verified.
- Activated `/opt/aipg-gallery-releases/gallery-815c11ee` through the release
  symlink and restarted both services at `2026-09-08T23:24:42Z`. Both are active
  with zero automatic restarts at verification. Backend PID `563942` resolves
  to the new release directory and its executable matches SHA-256
  `672a978fbdadb4a8c1c9b3f0c63d4bfd57ea20e625c0e9e9cd747d3e2a195c1e`.
  The shared environment hash is unchanged. Nginx was restored byte-for-byte
  to its pre-gate configuration; anonymous jobs and credits return 401 again.
- Public Studio and Director return 200. A fresh browser page restores the
  signed-in session after hydration, retains the earlier paid image, and shows
  purchased balance rounded to `$0.0007`. Krea correctly remains preview for
  this cohort. No additional paid generation was submitted during this release.
- Protected host evidence lives under
  `/var/lib/aipg-release-proof/gallery-815c11ee/`. The previous release is
  retained, but its image-processing dependencies are affected: do not blindly
  roll them back to undo a UI issue. Preserve the patches in any rollback build
  or explicitly contain the affected surface while repairing the candidate.
- Core remains on `c34c7da5` with charging allowlisted. No reward cutoff,
  global charging activation, payout restart, treasury refill, or historical
  economic-record rewrite accompanied this Gallery deployment.

### Gallery uncertain-outcome release: 23:43 UTC

- Gallery PR #24 merged as `9e7ff3dc2411d49d9bbd1cb0bc4b791fadd83adb`;
  its tree matches tested head `32bcc649`. CI `34291380273` and CodeQL
  `34291380245` passed. Local Go race tests/vet and all 98 frontend tests passed.
- The pre-fix tests reproduced a truncated response being accepted as success
  and a 504 body containing `404`/`unknown model` looking like a safe Director
  fallback. The client now checks body-read errors, validates the output count
  and presence, and classifies transport/gateway/malformed-result errors as
  unknown outcomes. Its public warning says the original may finish and be
  charged and retrying creates another generation. Definite 4xx rejections keep
  their existing handling; successful responses retain Core receipt metadata.
- This does not make pending jobs durable or recover results lost on restart.
  The client does not retry an uncertain request automatically, and its bounded
  public error cannot leak incidental upstream text into the recipe-fallback
  classifier. Paid Director remains gated on durable recovery and live tests.
- The host passed the frozen Node 22 build/reinstall, Go race tests, vet, binary
  build, and production dependency audit. The latest audit reports five moderate
  and one low finding, with no high/critical findings; earlier audit counts above
  describe their own observation time. Next `16.3.4` and sharp `0.35.4` remain.
- The candidate passed loopback Studio/Director checks, then activated at
  `2026-09-08T23:43:54Z` after a temporary submission gate and 45-second drain.
  The prior Go invocation showed zero generation starts/terminals across 30
  journal entries. Both processes run from `gallery-9e7ff3dc`; backend PID
  `565440` matches executable SHA-256
  `4c1c718c1a74f9d1db1576dcc39f88af6c6eff6da71193d7ae2cd2f34a9221fe`.
  Both have zero automatic restarts at verification. Patched `gallery-815c11ee`
  is retained for rollback, which avoids restoring affected dependencies.
- Shared environment is unchanged and Nginx was restored byte-for-byte. Public
  Studio/Director return 200; anonymous credits/jobs return 401 after the gate
  was removed. Host proof: `/var/lib/aipg-release-proof/gallery-9e7ff3dc/`.
  No paid generation, reward-policy activation, or payout accompanied this change.

### Media recovery candidate (not deployed)

- Core now has a candidate private result envelope committed atomically with
  the successful media reservation and completion row. It includes the whole
  verified output batch, rather than only a hash or a five-minute Redis event.
  Migration `0040` is additive and must precede the candidate application;
  historical results remain absent, and rollback retains recovery metadata.
- `GET /v1/media/results` retrieves by Grid job ID or the original progress
  token (`client_ref`) using generation's authenticated canonical account.
  Cross-account lookup is 404, reused ambiguous refs are 409, storage errors
  fail closed, and reads never retry work. No frontend needs direct DB access.
- Local SQLite and PostgreSQL 14.19 tests prove commit-before-DONE/ack,
  duplicate/release races, result-write rollback, owner/alias isolation,
  delegated HTTP authentication, result bounds and migration preservation.
  PostgreSQL 16 CI and the release backup/restore proof remain required.
- This is billed-result recovery infrastructure, not yet Gallery integration
  or request idempotency. Gallery must durably keep its own request handle,
  consult this result before retrying an uncertain paid stage, and preserve
  the recovered output through its normal history flow. Unreserved preview
  work and old completions have no reconstructed receipt; transient R2 object
  retention is unchanged. Paid Director and global charging remain unverified.

| Path | Core ownership / shared billing path | Live canary status |
| --- | --- | --- |
| Chat completions, including media shim | `routers/openai.py`, credits or media service | Paid Chat text/image and streamed-disconnect canaries reconciled; fresh paid text and empty-service 402 on 94be0cc1 |
| Responses | `routers/responses.py`, `_passthrough.py` | Paid stream/non-stream/disconnect reconciled; fresh empty-service 402 on 94be0cc1 |
| Anthropic messages | `routers/anthropic.py`, `_passthrough.py` | Paid stream/non-stream/disconnect reconciled; fresh empty-service 402 on 94be0cc1 |
| Image and image-to-image | `routers/images.py`, `services/media.py` | Paid single-image canaries passed; fresh empty-service 402. Img2img billing passed but visual QA incomplete: disabled |
| Video and image-to-video | `routers/videos.py`, `services/media.py` | Paid LTX and Director segments reconciled; fresh empty-service video 402. Timeline disabled |
| Audio | `routers/audio.py`, `services/media.py` | Paid Music result/reload reconciled; fresh empty-service 402 on 94be0cc1 |
| 3D | `routers/threed.py`, `services/media.py` | Disabled; no qualified live worker/canary |
| Batch images | One native request and full-batch hold; validate all output slots before settlement | Four-output canary failed cardinality and fully refunded; no worker reward. Disabled pending worker deployment and retest |
| Director first frame / segments / retries | Gallery orchestration into image/video routes | Multistage paid canary and reload recovery passed; local Gallery restart tests use a Core stand-in, not full Core crash recovery |
| Direct API, SDKs, provider integrations | Same public routes; verify no alternate dispatch bypass | Direct API paid/negative cases above; exhaustive external-consumer inventory remains open |
| Chat, Art, Music, Console | Delegated identity and shared purchased balance | One real linked account agrees across sites; signed-in reload and Console funding retry passed. Separate identity matrix remains open |
| Bots and direct service accounts | Explicit service identity and request/day ceilings | Four exact direct services are charged independently of model; codebase-design empty-service six-route 402/no-dispatch proof refreshed. Remaining consumer/key inventory open |
| x402 | Independent external payment proof; keep dark unless verified | Remains dark |
| Validator/worker setup probes | Bound dedicated no-DEN terminals; separate legacy coordinator sampler retired | Legacy sampler retirement deployed at `c34c7da5`; remaining live bound-path proof pending |

## Rollback boundary

Do not delete holds, ledger rows, payout rows, or nonce history. Preserve the
reward cutoff once activated. Stop generation on failing paths rather than
silently reopening unrestricted free traffic. Disabling charging only stops new
holds; existing holds still need settlement/release and monitoring. Keep payout
timers paused until reconciliation, even if demand charging is rolled back.
