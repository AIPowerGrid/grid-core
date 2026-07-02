# Verification Probes — coordinator canaries → validator consensus

**Status:** Phase 1 (coordinator-run, dark) shipping 2026-07-01. Phase 2 (validator
consensus) designed, not built. This doc is the durable plan.

## The problem

The coordinator trusts each worker's self-report of *which model it ran*, *what it
returned*, and *that it finished*. Nothing verifies any of it. That is fine for a free
network and **unsafe the moment real money flows**: a rational worker operator's best
move becomes "run the cheapest possible model (or a cache) and pocket the difference."
Verifiable compute — did the worker honestly run the model it claimed? — is the single
hardest and most valuable problem in decentralized inference, and today we have no live
answer. `grid_api/services/enforcement.py` + `VALIDATOR_V0.md` describe evidence with
no teeth; nothing populates it.

## The plan: one scoring engine, two trust models, in sequence

We verify the same *fact* two ways, in order:

| | Phase 1 — coordinator canaries (now) | Phase 2 — validator consensus (later) |
|---|---|---|
| Who measures | the coordinator (already trusted) | independent **staked** validator nodes |
| Trust model | centralized spot-check | decentralized, quorum |
| Economic weight | **none** (evidence only) | reward / slash |
| New assumptions | none | staking, quorum, dispute, slashing |
| Ships | today, dark | a quarter of work |

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

## Attestation record (`grid_validator_attestations`)

The schema already exists and was built for this (`canary_kind`, `nonce`, `verdict`,
`score`, `latency_ms`, `worker_id`, `model`, `modality`, `signature_status`). Coordinator
attestations set:
- `validator_wallet = NULL`, `signature_status = "unsigned"` — coordinator V0 doesn't sign
  (future staked validators sign with EIP-712 and set `signature_status="signed"`).
- `worker_id` = the worker that served the probe (from the job's `grid` provenance).
- `canary_kind`, `nonce`, `verdict`, `score`, `latency_ms`, `payload` = {prompt, expected,
  got} as evidence.
- `attestation_hash` = sha256 of the canonical record for idempotency.

**This pre-populates the exact table the validator network will later reach consensus
over.** That is the point.

## Restraint (why this is safe to ship today)

Mirrors the `GRID_CHARGING_ENABLED=0` and Validator-V0 patterns:
- **`GRID_PROBE_ENABLED` defaults OFF.** Deployed dormant, zero blast radius; flip on to
  begin collecting evidence.
- **Even when ON, evidence-only.** Attestations have **no** routing, reward, strike, slash,
  credit, or payout effect. A `fail` verdict changes nothing today — it is recorded and
  visible, nothing more. (There is nothing wired to consume verdicts, by design.)
- Conservative cadence (`GRID_PROBE_INTERVAL`, default 300s), tiny prompts (`max_tokens`
  ~24) so probe load on the GPU pool is negligible even in a 1-worker-per-model pool.

## Deployment status (2026-07-01)

**LIVE on prod, ENABLED, evidence-only.** `GRID_PROBE_ENABLED=1`,
`GRID_PROBE_INTERVAL=300`, `GRID_PROBE_MAX_TOKENS=256` in `/etc/aipg/grid.env`.
First attestations recorded (pass/fail/inconclusive) in `grid_validator_attestations`.

Deploy notes / learnings:
- **Prod deploy was NOT a git checkout.** Prod runs 848b3e6 + ad-hoc working-tree
  patches; the `validator_attestations` table was appended to prod `schema.py` and
  `create_all` built it on restart (prod DB is create_all, not alembic). probe.py was
  scp'd and the lifespan task added by a DIRECT edit to prod `main.py` — NOT the git
  patch, because the local probe commit (grid-core fd93fa2) accidentally bundled the
  *uncommitted* validator-router wiring (Jun-26 half-done feature) which prod has no
  file for; applying it crash-looped prod twice before this was isolated.
- **max_tokens must fit reasoning models.** 24 tokens got fully consumed by
  reasoning_content on gpt-oss → empty answer → false "inconclusive". 256 fixed it
  (gpt-oss-20b/120b/Gemma4 now pass 1.0).

### Follow-ups (known, not yet done)
1. **Redis leader-lock** — the loop runs in EVERY uvicorn worker (`--workers 4` → 4
   concurrent probe loops → 4× the intended rate). Add a Redis lock so exactly one
   worker probes. Harmless today (evidence-only) but multiplies with worker count.
2. **Local repo hygiene** — grid-core commit fd93fa2 bundled the accidental validator
   wiring; reconcile the local half-committed validator feature (validator.py,
   validators.py, schema change, 0006 migration are untracked) separately.
3. **Grader hardening** — add semantic grading / a judge model for open-ended canaries;
   current bank is deterministic factual (arithmetic, capitals) only.

## Future gates before verdicts get teeth (do NOT skip)

1. **Signed attestations** — EIP-4361/712 so an attestation is attributable and
   non-repudiable (coordinator can start signing before validators exist).
2. **Model-swap detection beyond canaries** — canaries catch a broken/garbage worker; a
   *smart* cheater runs a smaller model that still answers "17+5". Catching that needs
   logprob/perplexity fingerprinting or challenge-response a small model can't fake, and/or
   **redundant cross-worker execution** (same nonce to N workers, compare). Design before
   money depends on it.
3. **Quorum + dispute** — multiple independent validators must agree; a worker can dispute.
4. **Only then** does a verdict gain weight (reward multiplier / strike / slash), funded
   from the platform slice per `validator-rewards-design`.

## Honest external story

"Verification starts coordinator-run and progressively decentralizes to staked validators,
on a data path built for it from day one." Stronger than either "we're decentralized"
(false today) or "we have no verification" (also true today). See `GRID_ECONOMICS.md`,
`VALIDATOR_V0.md`, `validator-rewards-design`.
