# Text fidelity: measured baseline

Status: retained September 7 experiment independently re-verified September 11,
2026. This is a negative detection result, not a new live experiment or a
quality-authority rollout. No user prompts, production jobs or payments were
used. Private contexts, raw captures and decision thresholds remain private.

## What was checked

The independent auditor reconstructed the recorded results from source and
artifact hashes, the frozen pre-evaluation threshold, fresh context manifests,
native candidate output, reference token scores, and exact prefix bindings.
It also rehashed the retained 120B reference artifact and verified that the
owned reference process had stopped cleanly. It did not merely trust a runner's
summary or rerun generation until a desired answer appeared.

The study contains 288 candidate/control answers over 32 synthetic contexts:
256 answers from published 20B Q4/Q8 variants under serial/overlapping requests,
plus 32 serial 120B controls. There are 276 distinct reference-scoring calls;
identical prefixes may reuse a recorded score and are not independent trials.

The reference scored final-answer tokens conditioned on the candidate's
preceding output, including candidate reasoning. A correct explanation can
make the next answer token predictable to either model. Consequently this
feature does not necessarily retain information about which model generated
the reasoning in the first place.

## Held-out results

| Candidate | Recorded answers | Score available | Flagged |
| --- | ---: | ---: | ---: |
| 20B Q8 | 64 | 44 | 0 |
| 20B Q4 | 64 | 44 | 0 |
| 120B control | 16 | 13 | 0 |

Each 20B group produced 52 correct answers and 12 budget-limited answers in
this split; the 120B control produced 15 correct and one budget-limited answer.
Correctness and score availability are different measures. Missing scores and
budget limits are inconclusive, not cheating. The repeated candidates share
contexts and hardware; these rows are not 144 independent operator trials.

**Conclusion:** the tested feature detected zero substitutions in the eligible
held-out 20B observations. Zero flagged controls does not establish a fleet
false-positive guarantee either: this is one reference artifact/engine with
only 13 scoreable held-out controls. Do not promote this feature to routing,
reward, strike, bond or slashing authority.

Audited summary SHA-256:
`014a247517d69452ba1c35aac2a314a3d575c62085dd2902e7e089dac72b02a5`

Completed input-manifest SHA-256:
`5493cfd8cfcf775dff9480e48b57b7cc3871ad9ec2200f9b12692f4855998895`

## Fixed-context follow-up

A separate retained September 7 study was independently re-verified on
September 11. It tested the alternative of comparing distributions at the same
externally chosen raw context, without candidate-generated reasoning first.
The auditor checked identical tokenized/decoded contexts, runtime and model
artifact hashes, all native responses, the complete schedule, and an independent
implementation of the public coarsened top-20 probability-distance metric.
The public scorer and its existing bands matched the frozen manifest.

There were 64 distinct synthetic contexts, split into 32 calibration and 32
evaluation contexts across four families. Each of three model variants ran
each context twice: 384 actual calls, not 384 independent prompts. Both repeats
had exactly identical native distributions for every model/context pair.

| Evaluation comparison | Distinct contexts | Match | Gray | Anomaly |
| --- | ---: | ---: | ---: | ---: |
| 20B Q8 vs 20B Q4 | 32 | 31 | 1 | 0 |
| 20B Q8 vs 120B | 32 | 31 | 1 | 0 |
| 20B Q4 vs 120B | 32 | 30 | 2 | 0 |

All recorded positions were scoreable. These counts collapse the identical
repeats; gray means inconclusive, not fraud. At the tested bands, the rule
flagged zero cross-model substitutions. The same-model quantization comparison
had no anomalies either, but one engine and 32 contexts cannot establish a
fleet false-positive guarantee. Copying the reference distribution produces
zero distance by construction; the metric does not authenticate execution.

This rules out treating fixed contexts plus the current public distance bands
as a demonstrated solution to the final-answer failure. It does not prove that
all distribution-based methods are ineffective. This was a raw-context local
study, not a chat-template, public-worker, cross-engine or blind-routing trial.

Audited fixed-context summary SHA-256:
`c41e0fbb15146956086c9578f1e908be8de26de5c82b210e5a3e3442f79aed0a`

Completed fixed-context manifest SHA-256:
`6a1dc6e95737e055149b05b45263a2273bb04608ec1de9fb3c7029fd438054ff`

The independent audit reproduced the retained summary exactly. Its four
synthetic metric/eligibility tests also passed; those are implementation checks,
not additional model observations. No fresh generation or production mutation
was performed during this re-verification.

## Next experiment

1. Freeze a new synthetic corpus, model/runtime hashes, tokenization/context
   contract, calibration split, held-out split and decision rules before
   measuring the held-out set. Preserve failed and inconclusive observations.
2. Do not simply repeat fixed contexts with the same distance bands and call it
   progress. Use a calibration-only analysis to determine whether a proposed
   multi-context feature separates honest engine/quant variation from model
   substitutions. Freeze the feature, aggregation and decision rule before a
   fresh held-out evaluation. The already inspected studies are development
   evidence, not an untouched test set. Unsupported prompt-logprob APIs remain
   unsupported rather than receiving synthesized scores.
3. Include honest same-model controls across supported quants, engines, context
   templates and concurrency, not just one deterministic reference process.
   Count unique contexts and independent runs separately from repeated tokens.
4. Test weaker-model substitution, fabricated self-reported probabilities,
   replay and correct-model-only-for-probes behavior. Reference scoring must
   not depend on trusting candidate-reported logprobs as proof of execution.
5. Report detection, honest false positives and inconclusive coverage by
   model/runtime/attack. A threshold that catches weak models but rejects honest
   engines is not deployable. A template-specific success is not a broad claim.
6. Keep economic authority disabled. Worker-visible probe classification still
   needs the separately budgeted production-shaped audit lane and a measured
   held-out classifier test; same-context logprobs do not solve that problem.

See [anti-gaming gate](VALIDATOR_ANTI_GAMING.md) for the public-template attack
contract and [Responses qualification](VALIDATOR_RESPONSES_QUALIFICATION.md)
for the narrower evidence that native probability transport works. Neither
supersedes this negative detection result.
