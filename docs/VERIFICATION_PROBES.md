# Verification Probes — coordinator canaries → validator consensus

**Status:** Coordinator canaries plus registered-validator assignments, targeted
leases, and distinct-identity 3-of-5 quorum are production-live and evidence-only
on Core `0d850e73` with Alembic through `0024`. Three first-party pilot nodes
proved the protocol path on 2026-08-21, not independent operator control.
Economic validator authority, rewards, staking, routing effects, and slashing
are not live. Media assignments remain default-off.

## The problem

The coordinator trusts each worker's self-report of *which model it ran*, *what it
returned*, and *that it finished*. Nothing verifies any of it. That is fine for a free
network and **unsafe the moment real money flows**: a rational worker operator's best
move becomes "run the cheapest possible model (or a cache) and pocket the difference."
Verifiable compute — did the worker honestly run the model it claimed? — is the single
hardest and most valuable problem in decentralized inference. Coordinator
canaries provide a centralized evidence signal today; the shared-quorum preview
builds a distributed evidence path, still with no economic teeth.

## The plan: one scoring engine, two trust models, in sequence

We verify the same *fact* two ways, in order:

| | Phase 1 — coordinator canaries (now) | Phase 2 — validator consensus (later) |
|---|---|---|
| Who measures | the coordinator (already trusted) | registered validators, then independently operated bonded validators |
| Trust model | centralized spot-check | preview: distinct signed identities; future: independent quorum + disputes |
| Economic weight | **none** (evidence only) | preview: **none**; future: accepted-evidence reward / objective-fraud slash |
| New assumptions | none | preview: signed assignment binding; future: operator independence, bonding, disputes |
| Ships | live evidence-only | live evidence-only through `0024`; economic phase deferred |

**Build order rationale:** we ARE a centralized coordinator today; a coordinator that
spot-checks its own workers is coherent and needs nothing new. A validator network that
polices workers *before the coordinator itself is decentralized* is a roof without walls.
So the coordinator becomes **"validator zero"** — the first, trusted attester — and when
real validators come online they run the **identical scoring engine** against the
**identical attestation table**, just decentralized and staked. The trust model upgrades
underneath a stable data shape; nothing gets rewritten.

## The shared engine: `grid_api/services/probe.py`

Deliberately **who-agnostic** — it does not know or care whether the coordinator or a
validator is calling it:

- **Canary bank** — prompts with unambiguous, gradeable answers (arithmetic, exact-string
  facts), each carrying a fresh **nonce** in the prompt so a worker can't cache/replay a
  canned answer, run at `temperature=0` for determinism.
- **`grade(canary, text) -> (verdict, score)`** — verdict ∈ `pass | fail | inconclusive`,
  score ∈ [0,1]. V0 uses deterministic exact/normalized matching (no judge model needed).
- **`run_probe(model)`** — dispatches one canary through the *normal worker path*
  (`job_queue.submit_job` → `token_stream.subscribe_tokens`), so it measures exactly what
  a real request would get. Records latency + which worker served it.
- **`record_attestation(...)`** — writes to `grid_validator_attestations` (see below).

## Attestation and assignment records

Alembic `0006` owns the validator evidence schema:

- `grid_validator_attestations` stores evidence rows (`canary_kind`, `nonce`,
  `verdict`, `score`, `latency_ms`, `worker_id`, `model`, `modality`,
  `signature_status`) plus assignment-era fields (`assignment_id`, `grid_nonce`,
  `evidence_hash`, `authority`, `quorum_status`).
- `grid_validator_assignments` stores Grid-issued targets, nonces, challenges,
  probe results, and quorum lifecycle state.

Coordinator attestations set:
- `validator_wallet = NULL`, `signature_status = "unsigned"` — coordinator V0 doesn't sign
  (future staked validators sign with EIP-712 and set `signature_status="signed"`).
- `worker_id` = the worker that served the probe (from the job's `grid` provenance).
- `canary_kind`, `nonce`, `verdict`, `score`, `latency_ms`, `payload` = {prompt, expected,
  got} as evidence.
- `attestation_hash` = sha256 of the canonical record for idempotency.

External validator evidence can be stored in two authority tiers:

- `authority="preview"` — useful telemetry, visible in scorecards, but not
  assignment-bound.
- `authority="authoritative"` — only accepted when the submitted
  `assignment_id`, `grid_nonce`, target fields, and `evidence_hash` match a
  Grid-issued assignment and completed targeted probe.

**This pre-populates the exact evidence path the validator network will later
reach consensus over.** That is the point.

## Restraint (why this is safe to ship today)

Mirrors the `GRID_CHARGING_ENABLED=0` and Validator-V0 patterns:
- **`GRID_PROBE_ENABLED` defaults OFF.** Deployed dormant, zero blast radius; flip on to
  begin collecting evidence.
- **Even when ON, evidence-only.** Attestations have **no** routing, reward, strike, slash,
  credit, or payout effect. A `fail` verdict changes nothing today — it is recorded and
  visible, nothing more. (There is nothing wired to consume verdicts, by design.)
- `grid_api.services.router` does not query validator attestations. Current
  `auto` scores use only Grid-measured throughput and latency.
- Legacy coordinator canaries retain their conservative `GRID_PROBE_INTERVAL`
  cadence and tiny outputs. Shared-validator text probes are real GPU work,
  including calibrated 32K context requests, so Core separately limits new
  groups to one per worker/model per hour by default (with a five-minute hard
  floor).

## Shared-quorum validator API (live evidence-only preview)

Public capability discovery is live:

- `GET /v1/validator/capabilities`

The rest require a v2 account API key and are evidence-only:

- `GET /v1/validator/assignments` — joins a short-lived shared text probe group
  and issues a validator-specific nonce for the target worker/model.
- `POST /v1/validator/probe/{assignment_id}` — runs the assignment against the
  targeted worker path and records the Grid-side prompt/response hashes, private
  Core verdict, latency, and a bounded synthetic result envelope. The response
  omits Core's verdict. If the validator loses the response before signing, the
  same owner can request it again during the attestation window; Core returns
  the committed envelope with `replayed: true` without redispatching work.
- `POST /v1/validator/attest` — stores preview evidence, or authoritative
  evidence only when it matches the Grid-issued assignment, nonce, and evidence
  hash.
- `GET /v1/validator/scorecards` — aggregate worker/model evidence without raw
  payloads, nonces, signatures, account IDs, or validator identities. Objective
  text votes include Core-match/disagreement counts; media and preview verdicts
  remain labeled validator opinion.
- `GET /v1/validator/assignments/health` — assignment and quorum lifecycle
  health plus privacy-preserving agreement, dispute, coverage, and software
  version aggregates. It does not claim registered accounts are independent
  operators.
- `GET /v1/validator/workers` — current worker inventory for validator discovery.

Core requires three matching verdicts from five distinct registered validator
accounts within one worker/capability batch. Text v8 verdicts cover separate
randomized challenge instances, not byte-identical executions. Core records
quorum state (`pending`, `accepted`, `disputed`, `finalized`),
but the whole preview surface has `economic_effect: none`: no routing, reward,
strike, slash, credit, or payout effect. Text plus feature-gated image-fidelity
and video-contract assignment paths exist in the candidate. Video remains
default-off and proves only an objective output contract, not exact model
fidelity.

New `text.generated.v8` groups are capability batches, not identical-exam
groups. The batch fixes one target worker/model, capability, and concrete canary
kind, while every validator assignment stores a separately randomized challenge
and commitment. Already-open v7 groups finish with their original shared
challenge. Core prunes finalized assignment/group machinery after 90 days by
default while retaining signed attestations as the durable evidence record.
Completed envelopes exist only as operational crash recovery: maximum 512 KiB,
assignment-owner only, unavailable after that validator's authoritative vote,
and removed with the assignment machinery.
Workers advertising multiple text models rotate toward the least recently
covered model. Existing unfilled groups are joined before replacement groups,
and one validator cannot open duplicate groups for the same blocked model.

Text challenge families are selected cryptographically rather than from worker
ordering. Current candidate families are exact instruction, generated
arithmetic, strict JSON, calibrated 4K/16K/32K context retrieval, generated
multistep logic, and an exact single-function call plus a two-stage tool-call
chain with randomized names and arguments. Tool-call evidence commits the
witnessed text plus structured calls; Core and the node normalize each call
independently. The context lanes require conservative target-worker context
headroom so tokenizer differences cannot turn an advertised limit into a false
failure. The
second stage is hard-targeted to the same worker and receives the first tool
result through the ordinary OpenAI message contract. Longer chains remain
future work.
The stop-sequence family also verifies that the worker/backend honors one
randomized OpenAI `stop` value instead of merely following a prompt.
Group membership is capability-gated; legacy `text.basic.v1` nodes receive only
echo/arithmetic. Each validator normalizes the output and checks the one-way
expected-answer commitment locally instead of signing Core's private verdict.

Scorecards classify evidence into `availability`, `protocol_conformance`,
`capability`, `quality`, and `fidelity`. Echo, strict JSON, a single exact tool
call, stop-sequence, and token-limit checks are protocol conformance. Current
reasoning, context, code, and chained-tool probes are capability samples. No
current generated canary is quality-eligible, so a template-specific solver
cannot convert a passing echo or JSON check into a quality score.

Random values stop exact answer replay; they do not make a public template
unrecognizable. Core strips `_validator_*` metadata before WebSocket dispatch,
but recognizable prompts and the post-completion `den: 0` acknowledgment can
still label probes over time. This is an explicit economic-authority blocker.
Before evidence affects routing, rewards, or slashing, audit jobs must use a
broad production-shaped distribution and an ordinary bounded worker-payment
path whose dispatch and terminal acknowledgment do not reveal probe status.

The default-off sealed-assignment protocol removes another fingerprinting and
collusion surface: polling returns only an opaque assignment id, public
capability/lifecycle metadata, and a SHA-256 commitment. Target, model, nonce,
policy, and challenge are disclosed only after the worker's terminal result;
the node recomputes the commitment before signing. It is a staged compatibility
feature, so updated nodes ship first and Core remains unsealed until the fleet is
compatible. Sealing does not hide the prompt from the worker during execution
and therefore does not satisfy the blind-quality gate by itself.

## Deployment status (2026-08-22)

Coordinator-run probes remain live and evidence-only. Registered-validator
assignments, shared probe groups, and 3-of-5 quorum are production-live as
evidence only, not production authority. Do not publish validator binaries
against an older Core capability response.

Deploy notes / learnings:
- **Historical rollout note.** The July 1 hotpatch/create_all rollout was
  replaced on 2026-07-02 by deploying `system-core/main` at `63adc209` and
  running Alembic through `0006`. That was the first assignment schema rollout,
  not the current shared-quorum deployment state.
- **Existing prod DB needed a one-time Alembic bridge.** Because early validator
  evidence was created outside Alembic, prod was stamped at `0005` and then upgraded
  to `0006`. Do not repeat that stamp on databases that already have
  `alembic_version`.
- **max_tokens must fit reasoning models.** 24 tokens got fully consumed by
  reasoning_content on gpt-oss → empty answer → false "inconclusive". 256 fixed it
  (gpt-oss-20b/120b/Gemma4 now pass 1.0).
- **Run Alembic with the deploy user's HOME.** On prod, `sudo -E` preserved
  `HOME=/root`, which made asyncpg inspect `/root/.postgresql/postgresql.key`
  before connecting. Use `sudo -E -H -u aipg` for Alembic so HOME resolves to
  `/home/aipg`.

### Follow-ups (known, not yet done)
1. **Probe indistinguishability** — compensate bounded audit work through an
   ordinary worker settlement path, remove the retrospective zero-den label,
   add production-shaped blind synthetic workloads, and continuously test a
   template detector/model-switching worker. This gates every economic effect.
2. **Grader hardening** — add hidden code execution, longer tool-call chains,
   64K+ context tiers, and streaming-integrity checks. Keep judge models a
   supporting signal rather than an objective authority.
3. **Media/video validator lanes** — deterministic image fidelity and
   non-deterministic video contract paths exist in the dark candidate and remain
   economically inert. Video needs a governed recipe with explicit fps/timing
   metadata plus a real LTX canary before its independent operator gate is
   enabled. Reference-based `video.fidelity.v1` remains deferred.
4. **Economic gates** — do not attach routing, validator rewards, worker strikes, or
   slashing until assignment targeting, nonce-bound evidence, quorum, and dispute
   flows have been proven under load.

## Hardening shipped (2026-07-01, still evidence-only)

- **Signed attestations** — the coordinator ("validator zero") signs each attestation
  (ECDSA/EIP-191 over a canonical digest of hash+worker+model+verdict+score) when
  `GRID_PROBE_SIGNING_KEY` is set; records `signature` + `validator_wallet` (signer) +
  `signature_status="signed"`. Tamper-evident + attributable; future staked validators
  sign the same digest with their own keys.
- **Capability-tiered canary** (`hard_arithmetic`) — 2-digit multiplication is
  an objective capability sample. A failure is useful negative evidence, but a
  pass is not a model-identity or model-size proof: a script or tiny specialist
  can solve the public family. Evidence only.

## Model-swap detection — still needed (the hard part)

Canaries catch a *broken* worker and sample narrow advertised capabilities. They
do not establish model identity: a template router, specialist program, or
same-tier cheaper model can pass. Two stronger
detectors, both blocked on infrastructure we don't have yet:
- **Cross-worker consensus** — dispatch the SAME deterministic canary (temp=0, fixed seed)
  to N workers claiming the same model via `preferred_worker` affinity, compare outputs;
  an outlier is running something different. **Blocked: mostly 1 worker/model today** — no
  redundancy to compare. Build the mechanism so it activates when a 2nd worker appears.
- **Logprob / perplexity fingerprint** — a model has a characteristic token-logprob
  signature; compare the worker's returned logprobs to a reference. **Blocked: workers
  don't return logprobs by default; heterogeneous backends (vLLM/ollama/llama.cpp) differ
  numerically.** Needs a logprob-return contract + per-(model,backend) reference.
- **Throughput fingerprint** (cheap, noisy) — a swapped smaller model runs much faster;
  flag t/s far off the model's historical median. Weak alone (hardware varies), useful as
  a corroborating signal.

## Future gates before verdicts get teeth (do NOT skip)

1. **Signed attestations** — registered preview nodes already use EIP-191 over a
   canonical assignment payload. Any future contract-verifiable EIP-712 domain
   must preserve assignment, nonce, group, and evidence-hash binding.
2. **Model-swap detection beyond canaries** — canaries catch a broken/garbage worker; a
   *smart* cheater runs a smaller model that still answers "17+5". Catching that needs
   logprob/perplexity fingerprinting or challenge-response a small model can't fake, and/or
   **redundant cross-worker execution** (same nonce to N workers, compare). Design before
   money depends on it.
3. **Operator independence + dispute** — shared 3-of-5 protocol exists, but multiple
   independently controlled operators and a worker dispute process must be proven.
4. **Only then** does a verdict gain weight (reward multiplier / strike / slash), funded
   from the platform slice per `validator-rewards-design`.

## Honest external story

"Verification starts coordinator-run and progressively decentralizes to bonded validators,
on a data path built for it from day one." Stronger than either "we're decentralized"
(false today) or "we have no verification" (also false today). See `GRID_ECONOMICS.md`,
`VALIDATOR_V0.md`, `validator-rewards-design`.
