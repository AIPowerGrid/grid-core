# Validator failure audit: September 13, 2026

Status: read-only production diagnosis; versioned scoring correction is a
candidate, not deployed or present in preview.18. No historical verdicts,
operator reviews, balances, allocations or payments were changed by this audit.

## Scope and limits

The initial fleet snapshot reported 36 finalized failed groups. The retained
evidence audit selects groups created in the fixed UTC interval
`[2026-09-12T01:31:17Z, 2026-09-13T01:31:17Z)` that are finalized and failed at
query time. A subsequent read contains 37 groups and 185 assignments: fixing
the creation window does not freeze subsequent finalization. Do not describe
these as 185 independent trials or silently equate this later set with the
initial 36-group snapshot.

Queries use read-only PostgreSQL connections, a ten-second statement timeout,
and a 1,001-row limit with refusal above 1,000 retained assignments. Raw
synthetic prompts and responses are inspected only in private process memory;
this report contains aggregate classifications, not challenge material.
Each v8 assignment's own challenge is authoritative; the shared group challenge
is only a legacy fallback. Comparing all responses to the group commitment
would produce an incorrect audit for v8's distinct assignment challenges.

## Confirmed scoring defect

Nine failed groups contain 35 responses that repeat the exact committed marker,
stop with a length-style finish, and remain inside the existing independent
token-count window, but end with a proper prefix of that marker. The strict
v1 repetition check rejects that final partial marker. A requested generation
limit can naturally cut through a word; those responses should not be rejected
solely for that reason under the new rule.

| Reported model | Matching truncated-marker responses |
| --- | ---: |
| deepseek-v4-flash-nvfp4 | 7 |
| gpt-oss-20b | 12 |
| qwen3-27b | 12 |
| qwen38-flash-next-125b-nvfp4 | 4 |

These are retained response observations, not authenticated model identities,
new experiments, or proof that every affected group would change outcome.

## Failures not excused by this correction

- Smollm-135m accounts for 23 failed groups: 25 token-limit responses violate
  repetition, and 90 other responses have malformed task output. None matches
  the truncated-marker defect. Preserve the failures; this audit does not
  establish whether their cause is model capability, backend configuration,
  transport semantics or deliberate misconduct.
- Seven larger-model token-limit responses contain reasoning but no required
  visible output. They remain failed task evidence, not proof of model fraud
  or a native-tokenizer budget violation. Budget calibration needs separate
  measured tests rather than silently forgiving these results.
- One larger-model response stops early. Twelve tool-call/tool-chain responses
  are malformed, and four logic responses mismatch their commitment. These
  remain unresolved task failures, not evidence for automatic penalties.
- Structural inspection narrows the twelve larger-model tool failures: nine
  DeepSeek replies contain one call whose arguments are invalid JSON, with a
  stop finish and no visible text; three GPT-OSS-120B replies contain visible
  text and no call at the first tool-chain stage. SmolLM's 25 tool replies
  contain visible text, no call and a length finish. This does not distinguish
  backend emission from stream assembly defects: inspect an owned backend's
  raw deltas and their collected form before changing the scorer. Do not turn
  malformed arguments into a passing result through permissive JSON repair.
- Healthy individual replies can coexist with a failed group. Group outcomes
  must not be used to rewrite every individual verdict.

## Versioned correction and rollout

Core and the node add capability `text.token_limit.v2` and canary kind
`token.limit.v2`. Only this explicit version permits one final proper prefix
after at least two complete identical committed markers. Length-style finish,
minimum/maximum counts, reasoning accounting and expected-marker hash checks
remain unchanged. The original response, including its fragment, remains in
the response commitment and token count.

V1 remains unchanged for signed history and outstanding assignments. New nodes
retain v1 support while advertising v2 only with the tokenizer ready. Core
prefers v2 for their new groups without doubling the token-limit sampling lane;
older nodes cannot receive an unsupported v2 group. The distinct-assignment
batch contract remains `text.generated.v8`; scorer semantics are explicitly
bound by capability and challenge kind within that batch.

Required rollout: paired source review and CI, immutable Core deployment,
a new provenance-verified validator release, owned-node observation and then
identity-preserving operator upgrades. Preview.18 binaries do not acquire v2
retroactively. Compare fresh v1/v2 observations by policy without overwriting
old votes. All generated checks remain non-economic protocol/capability
evidence; this patch does not enable slashing, routing or worker rewards.

## Compensation remains separate

Correctly reporting a genuine failed assignment is useful audit work, not a
reason to disqualify its validator. Pilot eligibility must still verify the
signed assignment result, deduplicate work per reviewed operator/group and
respect an explicitly approved total and per-operator budget. A paid validator
does not gain authority to penalize workers. Operator independence review,
release eligibility, campaign creation and recipient consent remain necessary;
this scoring patch performs none of those operations.

See [compensation pilot](VALIDATOR_COMPENSATION_PILOT.md) and
[negative fidelity baseline](VALIDATOR_FIDELITY_BASELINE.md). Neither this
protocol correction nor higher compensation establishes model-substitution
detection.
