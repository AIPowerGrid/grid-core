# Proof of Quality — measuring intelligence, not trusting quants

> Centralized APIs ask you to trust the model behind the endpoint. The Grid is
> building a public evidence layer that measures delivered capability instead
> of trusting a precision label. The current validator preview records
> non-economic evidence; it is not yet a proof system with routing or slashing
> authority.

**Brand name:** *Proof of Intelligence.* **Technical name:** Proof of Quality (PoQ).

## The problem

On a decentralized network you can't trust what a worker *declares*. A worker
can claim it serves `llama-70b @ fp16, 128K context` while actually running a
gut-shot 2-bit quant at 8K. Quantization can be fine (a clean Q4 is often
indistinguishable) or ruinous (a broken low-bit quant fails structured reasoning
while still emitting fluent-looking text). Declared precision tells you nothing
reliable.

**So we stop trusting the label and measure the thing itself.** A great Q4 that
passes beats a "fp16" that fails. We sell *measured capability tiers*, not quant
levels.

## How it works

**Validator nodes** periodically consume private Core-issued assignments, send
hard-targeted probes to a worker+model, score the witnessed response
programmatically, and sign evidence. The current shared 3-of-5 preview stores
scorecards only. Future reviewed policy may use finalized evidence for routing,
reputation, and objective-fraud disputes.

### Probe batteries (objective and capability-gated)

Chosen because they degrade *sharply* under bad quantization while staying cheap
to grade by machine:

| Probe | Status | Grading |
| --- | --- | --- |
| Exact instruction and generated arithmetic | implemented candidate | committed exact answer |
| Strict JSON object | implemented candidate | JSON-only parse plus canonical commitment |
| Needle-in-haystack at the 4K tier | implemented candidate | committed exact retrieval |
| Generated multistep integer logic | implemented candidate | unambiguous numeric answer |
| Single and two-stage tool calling | implemented candidate | exact structured calls and committed transcript |
| Randomized stop-sequence handling | implemented candidate | exact pre-stop output |
| Hidden code tests and richer schemas | planned | sandboxed compile/test policy, not Core execution |
| Larger context tiers and token-budget honesty | planned | tokenizer-aware versioned policy |
| Same-request logprob/reference signal | production-deployed, disabled | supporting fidelity metric, never sole authority |

Structured output and tool use are useful quant-sensitive signals because their
contracts are machine-checkable. They still sample behavior; they do not prove
an exact model family or parameter count.

### Anti-gaming measures and limits

A static benchmark is trivially cheated (cache the answers). So:

- **Worker-visible transport looks ordinary** — validator markers, assignment
  ids, group ids, and nonces are stripped before dispatch, and worker-visible
  job ids use ordinary UUIDs. This raises the cost of fingerprinting but does
  not make probes literally indistinguishable from paid traffic.
- **Procedurally generated** — random chess positions, random needle facts,
  random seeds — plus a large, rotating probe bank. Nothing to pre-cache.
- **Validator-signed, random cadence** — unpredictable timing and origin.

The default-off `text.fidelity.v1` lane also compares a candidate's bounded
first-token probability distribution with one or two trusted workers serving
the same model. It uses the same sealed randomized request and deterministic
sampling parameters on every worker. One reference can only confirm
consistency; two agreeing references are required before a candidate can be
called an outlier. Missing logprobs or reference disagreement yields no
opinion.

This still is not ungameable. Workers control their software, can fabricate
logprobs, and can recognize or reroute a request that asks for them. The signal
therefore remains non-economic until production-shaped paid blind audits,
multiple independent references, and corroborating workload evidence make
probe-specific behavior materially harder.

### Scoring -> reputation -> economics

- **Now:** Core records assignment-bound evidence and separates probe completion,
  accepted evidence, worker pass, quorum, dispute, and finalization. No routing,
  reward, strike, payout, bond, or slashing path reads it as authority.
- **Next:** prove five or more independently operated validators and publish
  measured worker/model scorecards with explicit sample sizes and policy ids.
- **Later:** reviewed routing may prefer finalized high scorers. Objective fraud
  may enter a dispute process backed by cooldown-protected worker bonds. A
  subjective quality judgment must never slash automatically.
- **User-facing target:** agents choose a measured capability tier with recent
  evidence rather than trusting a worker's quantization claim.

### Hardware identification

Declared GPU specs (`nvidia-smi`) are a hint but spoofable. The trustworthy
signal is **performance fingerprinting**: a worker claiming an A100 that
benchmarks like a 3060 — on throughput (t/s), time-to-first-token, and
VRAM-bound batch limits — is misrepresenting, and gets flagged. Cryptographic
GPU attestation (e.g. NVIDIA confidential compute) is the trust-minimized
endgame but is hardware-limited; defer it. Near term: self-report + cross-check
against measured performance.

## Why it matters

- **For users/agents:** independently reproducible measurements can make the
  delivered service easier to audit than a private model label.
- **For the network:** makes quantization a non-issue — heterogeneous hardware
  and quants are fine as long as they *pass*. Maximizes usable supply without
  sacrificing quality.
- **For the token:** future cooldown-protected bonds can collateralize objective
  service contracts after dispute and enforcement paths are audited.

## Status & where it lives

- **Validator role** — candidate Core and validator implementations include
  wallet-bound registration, dedicated scopes, private assignments,
  hard-targeted text probes, independent scoring, signed durable delivery, and
  preview 3-of-5 groups. Core `96b5cf24` is production-live with Alembic `0034`;
  the matching source implementation exists in the validator repository, but
  the `text.fidelity.v1` lane is disabled and is not part of the frozen
  preview.13 cohort release.
- **Text methods** — exact instruction, arithmetic, strict JSON, context
  retrieval, multistep logic, single and two-stage tool calls, and randomized
  stop-sequence compliance are implemented as evidence-only policies.
- **Image/video** — design accepted, implementation gated. See
  `MEDIA_VALIDATION_V1.md`.
- **Bonding/slashing** — contract work exists, but cooldown-backed deployment,
  background bond sync, dispute review, and enforcement are not live validator
  authorities.
- **Telemetry** per job (throughput, TTFT, latency, model) exists as a supporting
  signal. It is not cryptographic hardware proof.

*Related: the validator-node role; worker bonding/slashing on the Grid
WorkerRegistry; per-model telemetry in `/v1/status/models`; GRID_ECONOMICS.md
(how measured quality ties into routing, tiers, and the stake).*
