# Responses Logprob Qualification

Status: local implementation and qualification, 2026-09-06. Not deployed.
No public assignment policy selects Responses. No scoring, reward, routing or
penalty authority is enabled by this work.

## Invariants

- Unsupported, missing, malformed and partial probabilities are observations,
  never proof of dishonesty.
- Assignment-bound text probes must branch before ordinary paid passthrough.
  They do not call settlement, award den, append worker ledger rows or change
  worker health/success metrics.
- Preserve native probabilities and bytes; do not renormalize a truncated top-k
  list or synthesize an absent first-token distribution.
- Keep job, worker, assignment and nonce binding separate from worker claims.
  Internal binding is stripped before worker dispatch and checked by the
  Responses targeted-stage receiver after replay.
- A visible-prefix hash is not a commitment to the complete conditional model
  context. Hidden reasoning, tokenizers and engine templates matter.

## Evidence

A synthetic local request used LM Studio 0.4.4+1, its
`llama.cpp-mac-arm64-apple-metal-advsimd@2.14.0` runtime and the
LM Studio community conversion of GPT-OSS-20B MXFP4. The pinned GGUF SHA256 is
`65d06d31a3977d553cb3af137b5c26b5f1e9297a6aaa29ae7caa98788cde53ab`.
This is not a claim that the conversion is tensor-identical to an upstream
OpenAI revision.

Streaming `/v1/responses` requested `message.output_text.logprobs`, top-k 5,
temperature 0 and at most 256 output tokens. Released text worker v0.3.8
relayed 26 events unchanged. Seven native probability positions were preserved.
The first visible word had no probabilities: coverage was partial.

The source/worker capture SHA256 is
`7e606e5624be36f30abaec997ae568f94c4e535b9b7640be59e4a9c7f28cf957`.
The private capture was replayed through a disposable, Unix-socket-only Redis:

```text
Core targeted-stage submit -> real Redis stream -> Core probe handler
  -> bounded Responses observations -> real Redis terminal/replay buffer
  -> Core targeted-stage receiver
```

All seven native positions were equal after this round trip, and the pending
queue entry was acknowledged. This was recorded-frame replay, not fresh
inference through the public Core, the registration handshake, the assignment
API, or a validator's signing/submission runtime.

Earlier negative LM Studio logprob observations applied to chat and legacy
completion endpoints. They must not be generalized to streaming Responses.
Neither successful transport nor enabling logprobs qualifies a worker as a
trusted model reference.

### Fresh Local Component Follow-Up

Two subsequent fresh requests traversed the released worker over a real
loopback WebSocket, this Core collector/Redis, and the independent validator
`responses_observation.py` checker. Both preserved seven native positions and
one missing first-word delta. The second private report SHA256 is
`763886b028471dac96641ee23647216484157c7458000486d55c6da24186ef83`.
It also proves EIP-191 recovery of a throwaway-key transport diagnostic and
rejection of that diagnostic by Core's network-attestation normalizer.

This advances fresh component transport beyond replay. It still bypasses the
production registration handshake, public assignment API, validator assignment
loop, outbox and attestation submission. No production credentials or state
were used. The new independent reader is not an advertised scorer; see the
validator repo's local `RESPONSES_QUALIFICATION.md` for its checks and limits.

## Implementation

- `validator_responses.py`: bounded per-event parsing and whole-stream
  accumulation, explicit missing-position gaps, output-item/sequence binding,
  visible-prefix hashes and distinct terminal availability.
- `worker_ws.py`: internal text-probe dispatcher before paid passthrough;
  dedicated Responses collector. Wall-clock and stream bounds cancel and close
  the socket to prevent late frames contaminating a subsequent job.
- `validators._run_targeted_text_stage`: explicit internal Responses selection,
  bounded streaming request and receiving-side provenance checks. No automatic
  endpoint fallback or modification of public assignment policies.
- The `responses-logprobs-observation.v1` envelope intentionally does not match
  the existing chat first-distribution scorer. `comparison_ready` and
  `quality_eligible` remain false.

## Verification

The focused reader/transport/Redis/existing-validator regression run passed
115 tests, including the private capture replay. The isolated Redis tests
require an installed `redis-server`; without it they skip rather than silently
substituting an in-memory store.
The broader router/service run passed 1,030 tests with 73 skips; those skips
remain gaps, including external-database-dependent coverage, not passes.

```sh
pytest grid_api/services/tests/test_validator_responses.py \
  grid_api/routers/tests/test_validator_responses_transport.py \
  grid_api/routers/tests/test_validator_responses_redis.py \
  grid_api/routers/tests/test_validator_worker_transport.py \
  grid_api/services/tests/test_validator_text_fidelity.py
```

The optional private replay reads `VALIDATOR_RESPONSES_CAPTURE`, a local test
artifact path, not a production setting. Normal CI still runs an independent
synthetic real-Redis round trip. Keep credentials and private challenge banks
out of fixtures and reports.

## Remaining Gates

1. A versioned assignment/witness contract and independent validator receiver,
   including commitment, signature, expiry, recovery and duplicate tests.
2. Fresh backend -> released worker -> staging Core -> validator runtime proof;
   recorded relay alone does not close that requirement.
3. Same-model engine/quantization baselines, pinned template/tokenizer metadata,
   repeated idle/loaded runs and investigation of reference-path timeouts.
4. Context-aligned meaningful token comparisons, reference-side answer scoring,
   held-out substitution tests and measured honest-worker false positives.
5. Adversarial forgery/replay/probe-switching/proxy/template/collusion tests,
   useful-work capabilities, media pilots and a reviewed shadow rollout.

Do not roll these observations into the old score or change public worker
reputation while those gates remain open. Compensation is a separate reviewed
budget decision, not evidence that a validator can penalize workers.
