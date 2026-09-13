# Validator failure audit: September 13, 2026

Status: read-only production diagnosis and bounded owned-backend follow-ups.
The versioned token-limit correction is deployed in Core `d606e4d8` / Alembic
`0042` and validator preview.20, not present in preview.18. No historical verdicts,
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

Core and preview.20 are deployed, with reviewed source, four-platform release
qualification, exact-source artifact provenance and owned-node observation.
All three owned nodes preserved identities and journals; fresh v2 and completed
empty-visible evidence passed signature/binding checks without economic rows.
Website PR79 promoted .20. External operator upgrades remain necessary.
Preview.18 binaries do not acquire v2 retroactively. Compare fresh v1/v2
observations by policy without overwriting
old votes. All generated checks remain non-economic protocol/capability
evidence; this patch does not enable slashing, routing or worker rewards.

## Stop Follow-Up

Two newer owned Qwen stop assignments were compared with six direct backend
calls. Original stop, streamed and non-streamed, yielded reasoning but no
visible answer. No-stop controls produced the expected normalized answer
prefixes and included the stop marker inside reasoning. Original budgets were
1,024 tokens, with only 51/58 completion tokens consumed in the stored jobs.

Four additional calls used a documented per-request non-reasoning control.
Both stop-present replies exactly matched the expected answer; both no-stop
replies continued through the marker and suffix. This qualifies those two
cases on the owned backend, not all engines or a new production-wide option.

Core currently retains `reasoning_text` only for token-limit v1/v2 probes.
Null reasoning in stored stop evidence therefore does not establish zero
backend reasoning. The preview.20 empty-visible delivery fix remains valid;
do not use that canary as evidence that no reasoning was generated.
See the [bounded stop diagnosis](https://github.com/AIPowerGrid/grid-validator/blob/fedd431ef563bc91419dd1512ce9c447ab19b272/STOP_REASONING_DIAGNOSIS.md)
for capture hashes, controls and versioned-evidence follow-up requirements.
These newer stop cases are not additional members of the original 36-group
snapshot and must not be added to its denominator.

## Tool-Format Follow-Up

A new read-only query over the original fixed creation window retained the
same twelve larger-model failed tool assignments: nine DeepSeek results whose
arguments contain native tool markup rather than JSON, and three GPT-OSS-120B
first-stage replies with text but no tool call. No raw challenges are public.

Six sequential direct calls to the currently configured owned DeepSeek backend
reused one single-call assignment and one chain assignment's **first stage**.
Each retained its original prompt, tools and output budget, with a 45-second
request timeout. Named-tool streaming/non-streaming and auto-tool streaming
were compared. This is two reused cases, not six independent trials or a
completed two-stage tool-chain test.

| Variant | Single-call case | Chain first-stage case |
| --- | --- | --- |
| Named tool, streaming | JSON arguments, no visible text | Native markup in arguments, invalid JSON |
| Named tool, non-streaming | Native markup in arguments, invalid JSON | Native markup in arguments, invalid JSON |
| Auto tool, streaming | JSON arguments plus native markup in visible text | JSON arguments plus native markup in visible text |

All six returned HTTP 200. An independent parser verified that each of the
three malformed argument strings was byte-identical to its corresponding
historical Grid result. Non-streaming failures demonstrate that Grid stream
assembly is not required to reproduce this defect. Native backend output is
already malformed for the API contract. The endpoint subsequently reported
`0.1.dev1+gedc82b614`; that suffix resolves to upstream vLLM commit
`edc82b614f51f4f9ce16c7010e879571e5c46136`. This HTTP metadata does not verify
the running image, launch flags or local patches. Do not claim a particular
upstream parser patch repairs it, or that all nine historical calls had one
independently witnessed cause.

The private capture SHA-256 is
`ac5b56714582d7f2ed010f0b3e58e9beeacfa3bda5ddfae3a174c9eb7d63df8f`.
Both auto-mode replies also leaked native tool markup into visible content;
switching every validator request to auto is not a verified repair. The one
successful named streaming sample likewise does not prove a reliable endpoint.

Next: inspect the actual backend parser/template version, reproduce with a
public synthetic fixture, then qualify named and auto calls in streaming and
non-streaming modes plus a complete two-stage chain. Require valid JSON,
correct names/arguments, and no leaked markup before changing the backend.
Keep strict validator scoring; do not repair arbitrary worker JSON to turn a
failure into a pass. The GPT-OSS comparison below diagnoses its no-call cases
separately. No worker service, runtime configuration, score or economic record
changed during these tests.

## GPT-OSS No-Call Follow-Up

Six direct requests reused two of the three retained GPT-OSS-120B first-stage
tool-chain cases. Named-tool streaming/non-streaming and auto-tool streaming
were compared at the original 1,024-token budget and temperature zero. These
are two repeated first-stage cases, not six independent tests or a complete
two-stage workflow.

| Variant | Case A | Case B |
| --- | --- | --- |
| Named tool, streaming | Text, no call | One correct structured call |
| Named tool, non-streaming | Text, no call | Text, no call |
| Auto tool, streaming | Text, no call | Text, no call |

All six returned HTTP 200. In the three failing named-tool responses, ordinary
visible text contained the correct argument object. An offline comparison
using the original step commitment confirms the values, but this is **not** an
actual tool call and must not be promoted to one by the validator. One
non-streamed response was byte-identical to its historical stored visible text.
The one actual structured call matched the original first-stage commitment.
Five missing calls and one success demonstrate an intermittent backend contract
failure, not model incapability or a Grid-only stream-assembly defect.

Private capture SHA-256:
`de4c6d5fce0d62f9643f66b02912ae07d47dd62c9bd4d214788c7f4ea37da390`.
An earlier helper attempt incorrectly sent an empty Authorization header and
failed locally in HTTPX on all six attempts before obtaining an HTTP response.
It is retained separately and excluded from the six real backend requests.
The corrected helper omits an absent key, matching the deployed worker.

The GPT endpoint's `/version` reports vLLM `0.10.2`; its port number is not
evidence that it runs Ollama (`/api/version` returned 404). The
[published v0.10.2 serving source](https://github.com/vllm-project/vllm/blob/v0.10.2/vllm/entrypoints/openai/serving_chat.py)
handles GPT-OSS/Harmony before the generic named-tool branch, extracting calls
from the generated Harmony channel/recipient. This supports inspecting the
actual serving/parser setup rather than assuming generic named-tool guarantees
apply. Endpoint metadata and upstream source do not prove the deployed image
or absence of local patches. Existing SSH authentication to the backend was
rejected; no alternative credentials, host changes or restarts were attempted.

Next: obtain the correct host access, inspect launch flags and artifact version,
and test a pinned candidate backend with ordinary chat, named/auto tools,
stream/non-stream parity and a complete two-stage chain before any rollout.
Do not silently rewrite content as calls, relax scoring, or change model
identity claims to conceal this protocol failure. Compensation remains separate.

## Independent Logic Oracle

A read-only bounded query recovered the original four larger-model multistep
logic failures, all reported as `qwen38-flash-next-125b-nvfp4`. A separate parser
accepted only the documented start value and four ordered add/subtract/multiply
operations, then computed the answer independently without calling Core's
generator or trusting its expected value. All four computed hashes matched
their assignment's expected-answer commitment.

Each retained reply was a syntactically valid but incorrect integer under the
released normalizer. All finished with stop and reported only 3-5 completion
tokens. These are wrong-answer observations, not formatting-only rejection,
reasoning exhaustion, an incorrect Core oracle, or a demonstrated transport
failure. No new model call was made. The reported model name remains unverified;
these observations do not establish substitution or deliberate misconduct.

Private capture SHA-256:
`9962139bfe84a31031d72245d1cca1a2e4fc092594eacb83708ca16c0cee8708`.
Keep these four original failures; no historical score was rewritten.

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
