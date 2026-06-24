# Proof of Quality — measuring intelligence, not trusting quants

> Centralized APIs ask you to *trust* the model behind the endpoint. The grid
> *proves* it. Every model is continuously benchmarked for real intelligence —
> measured capability, not a precision label.

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

**Validator nodes** (see the validator role) periodically and unpredictably send
**probe batteries** to every worker+model and score the responses
programmatically. The score drives routing, reputation, and — with a stake at
risk — slashing.

### The probe batteries (auto-gradeable, quant-sensitive)

Chosen because they degrade *sharply* under bad quantization while staying cheap
to grade by machine:

| Probe | What it catches | Grading |
| --- | --- | --- |
| **Structured generation** — render a chess FEN to SVG, emit JSON to a schema, write code that compiles | spatial/structural collapse (the first thing low quants lose) | does it parse / compile / match expected? |
| **Reasoning** — math & logic with verifiable answers | degraded reasoning under quant | exact-match on the answer |
| **Needle-in-haystack** — bury a fact at depth N, ask for it | also **verifies the claimed context length** is real | did it retrieve the needle? |
| **Instruction following** — strict output format | quant-induced format drift | regex / structural check |
| **Perplexity on reference text** | continuous low-level degradation signal | numeric threshold vs the reference model |

The SVG-chess / structured-output class is the sharpest tell: a heavily
quantized model produces confident garbage that a parser rejects instantly.

### Ungameable by construction

A static benchmark is trivially cheated (cache the answers). So:

- **Probes are mixed into real traffic** — a worker can't tell a paying request
  from a test.
- **Procedurally generated** — random chess positions, random needle facts,
  random seeds — plus a large, rotating probe bank. Nothing to pre-cache.
- **Validator-signed, random cadence** — unpredictable timing and origin.

### Scoring → reputation → economics

- Each **worker+model** carries a rolling **quality score** and a measured tier
  (effective context length, structured-output pass rate, reasoning accuracy).
- Routing **prefers high scorers**; persistent failers are **downranked, then
  evicted**.
- The teeth: this is where PoQ meets **worker bonding/slashing**. A worker stakes
  AIPG to serve; serve measured garbage and the stake is **slashed**. Quality is
  not a request — it's collateralized. *(This is the real, load-bearing AIPG
  utility: skin-in-the-game on delivered intelligence.)*
- The measured tier is **surfaced to users and agents** — you pick a capability
  tier, not a quant you have to trust.

### Hardware identification

Declared GPU specs (`nvidia-smi`) are a hint but spoofable. The trustworthy
signal is **performance fingerprinting**: a worker claiming an A100 that
benchmarks like a 3060 — on throughput (t/s), time-to-first-token, and
VRAM-bound batch limits — is misrepresenting, and gets flagged. Cryptographic
GPU attestation (e.g. NVIDIA confidential compute) is the trust-minimized
endgame but is hardware-limited; defer it. Near term: self-report + cross-check
against measured performance.

## Why it matters

- **For users/agents:** continuously-verified quality is something **no
  centralized API offers** — they ask for trust; we publish measurements.
- **For the network:** makes quantization a non-issue — heterogeneous hardware
  and quants are fine as long as they *pass*. Maximizes usable supply without
  sacrificing quality.
- **For the token:** PoQ is the mechanism that makes the AIPG stake meaningful —
  collateral against measured intelligence, enforced by slashing.

## Status & where it lives

- **Validator role** (the prober/scorer) — to build; see roadmap.
- **Bonding/slashing** (the economic teeth) — in progress.
- **Telemetry already captured** per job (t/s, TTFT, latency, per-model) feeds
  the performance-fingerprint side today; the probe battery + scoring is the new
  build.

*Related: the validator-node role; worker bonding/slashing on the Grid
WorkerRegistry; per-model telemetry in `/v1/status/models`; GRID_ECONOMICS.md
(how measured quality ties into routing, tiers, and the stake).*
