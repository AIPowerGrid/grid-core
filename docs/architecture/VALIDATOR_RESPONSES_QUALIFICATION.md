# Responses Logprob Qualification

Status: local implementation and qualification, 2026-09-06. Not deployed.
No public assignment policy selects Responses. No scoring, reward, routing or
penalty authority is enabled by this work.

Detection is a separate gate: the retained answer-logprob experiment found no
substitution flags in eligible held-out 20B cases against its 120B reference.
See [the independently re-verified baseline](VALIDATOR_FIDELITY_BASELINE.md).
The transport evidence below must not be presented as a successful detector.

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

### End-to-End Fault Qualification

A local synthetic SSE backend exercised the unchanged released v0.3.8 worker
over actual HTTP and WebSocket sockets, the Core collector, disposable Redis
queue/replay, and the independent validator reader. These are transport fixtures,
not native model outputs or public signed assignments.

The first 18-case run found four mismatches: duplicate probability/event JSON
keys were accepted, and over-byte-limit or invalid-Unicode item IDs were labeled
available even though the validator rejected them. Core now rejects duplicate
keys at any depth and checks item IDs against the reader's UTF-8 byte contract.
Seven regression cases cover those failures, nested alternative duplicates,
and valid identifiers exactly at the byte limit. Five failed before the fix.

The final formatted source passed all 18 transport cases: two valid controls,
missing/empty/malformed/over-count probabilities, positive/boolean/NaN values,
both invalid identifiers, duplicate keys, replay, missing/extra terminal events,
and oversized event/text payloads. Invalid cases became unavailable, never
failed-worker scores. Oversized streams closed their test connection; a later
control passed on a new connection. This is not same-socket recovery proof.
Queue entries were acknowledged, Redis replay matched the collected envelope,
and guarded economic/health functions were never called. The owned Redis and
test listeners stopped. Production workers and state were not changed.

Private evidence SHA-256:

- Original failing run:
  `b213b15e8ec4b0f2d8bb8cc9bbc2e64267245b9b5e13bc21b95f5971a6d6a9d4`.
- Final formatted-source fault run:
  `5c5b74e29810779a193c039b15a02fc66077ba846d0a59a7c2d81959403ccf8a`.
- Fresh LM Studio follow-up after the logic fix:
  `0d876097e861cdc858aa907b075eb7a681c8d78dc8ae59a579aa8cb4a9de7e03`.
  It preserved seven native probability positions and one explicit missing
  delta, and again rejected the throwaway diagnostic as a network attestation.

The final focused five-file regression suite passed 122 tests with no skips,
including the optional private capture replay. Ruff and Black checks passed.
The full Grid suite and public assignment/signing loop were not rerun by this
narrow parser fix; the remaining gates below still apply.

### Components

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
