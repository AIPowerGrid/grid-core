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

## Next experiment

1. Freeze a new synthetic corpus, model/runtime hashes, tokenization/context
   contract, calibration split, held-out split and decision rules before
   measuring the held-out set. Preserve failed and inconclusive observations.
2. Compare reference and candidate distributions at the same externally chosen
   context positions, without first feeding candidate-generated reasoning into
   the reference. Treat this as an experiment, not an assumed fix. Unsupported
   prompt-logprob APIs must remain unsupported rather than synthesizing scores.
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
