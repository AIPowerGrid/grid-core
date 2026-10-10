<!-- SPDX-FileCopyrightText: 2026 AI Power Grid -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# LTX-2.5 Video Integration

## Status

Candidate implementation, not a production release. Four versioned BF16 graphs
live in `recipes/qualification/`, which startup does not load. The default is
still `LTX-2.3`; no public model advertisement, price, worker enrollment,
generation gate or charging setting is changed by this candidate.

Two matching native GPU-pinned ComfyUI instances have completed concurrent
local distilled renders on the owned dual-Blackwell rig. ComfyUI remains
loopback-only, and each GPU has separate writable state. The stopped VLO
container and prior services are preserved for rollback. Local qualification
does not establish Grid billing, x402, execution attestation or independent
validator evidence.

## Governed Contract

The prospective model IDs are `LTX-2.5 Distilled BF16` and `LTX-2.5 Dev BF16`.
Each has a separate text-to-video and image-pin recipe. `_grid.videoContract`
selects the strict `aipg.ltx-video.v1` contract; clients never submit graphs or
choose checkpoint filenames. Graphs use the installed LTX-2.5 BF16 transformer,
Gemma4-12B projection encoder and LTX-2.5 video/audio VAEs.

| Input | Distilled | Dev Candidate |
| --- | --- | --- |
| Default size | 960x544 | 960x544 |
| Size band | 256..960 per axis, multiples of 32, at most 522240 pixels | Same |
| Frames | 9..337, exactly 8n+1; default 81 | 9..81, exactly 8n+1; default 81 |
| FPS | 24 only | 24 only |
| Steps | 8 only, fixed sigma schedule | 4..40; default 40 |
| CFG | 1 only | 1..5; default 2 |
| Sampler | euler_ancestral_cfg_pp | Same |
| Audio | Generated synchronized audio, always on | Same |
| Images | Up to 16 inline PNG/JPEG/WebP pins; first at frame 0 | Same |

Explicit `frames` are never adjusted. `seconds` alone selects the nearest
8n+1 frame count; effective duration is `frames / fps`. Four requested seconds
become 97 frames / 24 fps, or approximately 4.041667 seconds. If both fields
are supplied, they must already describe the same duration. Cost estimates,
reservation units, worker payload, graph and `grid.execution` use that effective
duration. Omitted seed randomizes once; explicit seed zero is preserved.
Chat media uses the same 81-frame recipe default when no timing control is
available, rather than imposing the legacy four-second default on dev.

The image form accepts either `image` or typed `keyframes`, never both. Pins
start at zero and must be unique, strictly increasing and within the clip;
strength is a finite value in 0..1. Core expands only its governed scaffold,
freezes the image bindings with the graph and requires a matching source list
before reserve. All inline images decode before any upload or quota consumption.
Total inputs are bounded to 32 MiB encoded / 24 MiB decoded, with the existing
12 MiB per-image limit; repeated images share one source object.
Multi-pin jobs require `video`, `image-to-video` and `video-timeline` admission,
plus a worker advertising `recipe-image-bindings-v1`. Compatibility is checked
before reserve and again at dispatch; old workers never receive those graphs.
This is a transport compatibility claim, not a worker-fidelity attestation.
LoRAs, timeline strings, unsupported samplers/schedulers,
precision changes and disabling generated audio also reject instead of being
ignored. Incoming source aspect is fitted under the same pixel ceiling when
size is omitted; an explicit size remains authoritative.

The legacy video model keeps its 1..10 second, maximum-240-frame contract and
four-second HTTP default. It does not acquire LTX-2.5 frame/keyframe authority.

## Economic Invariants

1. Before a monetary hold, Core resolves one approved graph and freezes its
   execution units. It does not re-resolve that graph after reserving funds.
2. Steps and frame count in the ordinary worker reward payload agree with the
   graph, rather than generic diffusion defaults. A recipe still does not prove
   what an untrusted worker executed.
3. Normal media reserve/terminal settlement/release remains authoritative.
   HTTP timeout or disconnect is not cancellation. This change does not alter
   SQL money guards or introduce an HTTP collector as a second settler.
4. `/v1/account/credits/quote` accepts frames for contract-backed video and
   reports effective frames/FPS/duration. Unknown media rates remain unpriced,
   not a free-job exception. These model IDs have no price yet.

The public rate still needs review against measured execution cost and the
funded-worker reward policy. A dev rate cannot silently inherit the distilled
rate. Do not activate either candidate until pricing and real durable lifecycle
canaries pass. Changing new-request charging cannot reprice existing holds.

### Generating Shot References Through Grid

Shot images use the existing `/v1/images/generations` endpoint and image workers,
not a hosted-provider fallback or a new graph-execution API. The local Director
CLI candidate can request a single PNG, recover its billed result by job/client
reference and save a committed-hash-checked asset as a relative LTX timed pin.
Model selection and any source-image edit remain governed by image recipes and
generation admission. This does not open video-timeline admission.

The companion image request adds optional strict `max_cost_micro_usd`
(0..2^63-1). In live charging, `authorize_media` compares the discounted total
with it before service exposure or promotional/free/purchased holds. Exceeding
it returns 402 before dispatch. Omission preserves existing callers; preview
remains zero-charge. `/health.features` advertises
`image-max-cost-micro-usd-v1`, because older image schemas accept unknown fields
without enforcing them. Clients must not infer safety from a quote alone.

This ceiling bounds one image request, including any batch's total, not the
entire film. A progress/client reference is not a dispatch idempotency key;
recovery must never automatically resubmit an uncertain request. CLI receipts
and tests are implemented locally, but live Grid-image-to-LTX qualification,
project budgets, release and video x402 remain outstanding.

Focused image-cost/quote tests passed 68 cases locally. The four additional
shot-image tests passed on SQLite and disposable PostgreSQL 14: increased-price
denial leaves funds/dispatch untouched, and exact-ceiling success persists one
charge and result under duplicate terminals. The full collected local Core
suite passed 2057 tests / 275 skipped; those four added cases ran separately
after collection. Prior snapshot `39d1f552` passed required PostgreSQL 16,
Redis and cross-repo CI. The image-cost follow-up requires fresh CI before release.

## Evidence And Limits

Owned-rig local tests on October 10, 2026:

- Concurrent distilled 960x544 / 81-frame renders decoded correctly as H.264,
  24 fps, 3.375 seconds, with 3.33 seconds of stereo audio. One cold run took
  55.2 seconds and one warm run 18.4 seconds; these are not throughput guarantees.
- A first-image distilled render completed in 22.5 seconds and passed structural
  checks. Broader image/aspect quality coverage remains required.
- Distilled 960x544 / 297 frames completed in 83.1 seconds, with 12.375 seconds
  of video and 12.33 seconds of audio. Sampled frames showed coherent movement.
- Three-guide dev and distilled tests completed but produced frozen sections;
  structural output success did not qualify guide quality.
- Core-resolved dev text-to-video completed in 88.1 seconds and dev first-image
  in 82.2 seconds, both 960x544 / 81 frames / 24 fps, with 3.33 seconds of stereo
  audio. No one-second freeze was found by the configured diagnostic. Both used
  the real worker graph adapter; first-image also used its real source download
  and ComfyUI upload. The output namespace alone was changed for private QC.
  The first-image result blended the reference scene with the prompted new
  subject, so this does not qualify reliable character replacement.
- The handoff meadow and dev action were rerendered concurrently through the
  candidate Core resolver and actual multi-image worker adapter. The meadow used
  seven pins / 337 frames and completed in 126.8 seconds on GPU0; dev action used
  first/last pins / 33 frames / 40 steps / CFG 3 and completed in 39.1 seconds on
  GPU1. Both decoded as 960x544 / 24 fps with stereo audio (14.01 and 1.33
  seconds), matching their exact video units. Neither triggered the configured
  one-second freeze diagnostic. Core/CLI graphs matched after binding inputs;
  only the output namespace changed. These used the manifest prompt, references,
  seed and global negative prompt, not an assertion of pixel-identical original
  takes. Source serving was private loopback, not R2 or Grid WS.

Raw inventory, model hashes, exact graphs, prompt IDs and generated media remain
in the ignored private operator evidence, not in public fixtures. Maximum
workload settings are conservative admission ceilings, not an exhaustive
resolution/frame/hardware qualification. Broader dev text/first-image quality,
cross-GPU warm/cold recovery and the full Grid WS/R2 path remain unqualified.

Sixteen-pin capacity stress tests completed at 960x544: distilled / 337 frames
in 137.5 seconds and dev / 81 frames in 202.4 seconds. Exact frame count and
synchronized stereo audio passed. Unrelated references were densely mixed;
distilled showed scene blending and dev showed severe block/noise artifacts in
sampled frames. The dev stress case fails visual quality despite structural
success; it is not evidence for public dense-guide qualification. The separately
successful handoff two-pin dev action remains the relevant film baseline.

Candidate-specific money tests run real SQL holds and the ordinary worker
terminal with synthetic rates, transport, storage and free pockets. Disposable
local PostgreSQL 14 tests pass successful/insufficient/unpriced paths, duplicate
terminals, late success after refund, result-write rollback, HTTP timeout/result
recovery, 20 competing completions and reserve overdraft races. This is not a
real payment or GPU/storage canary; required PostgreSQL 16 CI remains separate.

## Reference-Film Editing Benchmark

The maintainer identified the [reference-film recreation example](https://www.reddit.com/r/StableDiffusion/comments/1x168pn/minimax_h3_viral_zombie_short_film/)
as the product inspiration. Its published graph uses character/background images
and timed shot descriptions, not source-video conditioning. The soundtrack
script reuses source audio with segmented tempo changes. Those model-specific
results do not establish LTX-2.5 feature parity.

The Director acceptance path is:

```text
operator-provided source film -> cut/frame analysis -> reviewed shot manifest
replacement references -> scene/shot generation -> take selection -> picture edit
original audio + explicit timing map -> final export + recovery receipts
```

Distinguish `preserve` (unchanged timing; stream-copy compatible audio) from
`retime` (explicit tempo mapping; decoded/re-encoded audio) and `generate`.
Never label retimed audio bit-identical. Reference-video motion transfer,
multiple-character references and native timestamped cuts need independent
LTX qualification. Local FFmpeg assembly must not imply those capabilities.

### Existing LTX Film Baseline

The maintainer's handoff already contains a completed LTX-2.5 production, not
just the H3 inspiration: a film manifest, prompts, keyframes, three dumped
workflows, editing/soundtrack scripts and a reference picture cut. Its selected
takes and finished soundtrack export are still present on the owned rig.
Read-only decoded-frame checks on October 10 confirmed a 960x544 / 24 fps final
with 622 frames (25.916667 seconds), plus the selected 297-frame cabin,
337-frame meadow, 33-frame dev action and 81-frame opener takes with audio.

That working production is the integration baseline. The cabin uses ten image
pins, the meadow seven, the action first/last pins with dev CFG 3, and the opener
a softened middle pin. The candidate now carries these typed pin chains and
the 337-frame meadow workload; this does not yet prove a complete Grid render,
film export, billing or storage canary. Generic three-guide test failures describe those particular
tests, not an inability of LTX to produce the maintainer's successful film.
Preserve and port the known workflow rather than substituting the simpler
qualification graph and claiming equivalent functionality.

The local builder's inference dependency graph matches all three supplied
workflow dumps after normalizing node IDs and input-upload names. Output prefix
and explicit MP4/H.264 encoding replace the prototype's output-auto settings.
This structural comparison is not a rerender or a complete film/export proof.

Reconcile the handoff's nominal 25.958-second acceptance target with the
622-frame final before encoding a frame-exact export test. The prototype also
contains hardcoded source paths and editing details that must become validated
manifest data, not executable client input. Original soundtrack preservation
is already represented by its fixed-length shot splice; segmented tempo
mapping is the separate retimed-export path.

Before old-path retirement, reproduce this LTX baseline through the intended
CLI and Grid path: character consistency, cut accuracy, useful motion, audio
alignment, render cost, failed-take budget and interrupted-request recovery.
Use authorized source media and private evidence; do not publish the handoff's
private prompts or media as test fixtures. H3 is an inspiration/comparison, not
a required fallback or evidence that the working LTX approach must be replaced.

## Remaining Release Gates

- Compare and execute the Core-resolved graphs through two enrolled one-job
  media-worker identities. Verify per-GPU state, result storage and exact units.
- Reproduce the handoff's multi-pin/337-frame jobs through the intended network
  path before describing the candidate as the full Director contract. Promote bounded
  settings from the working baseline with separate money/storage proofs.
- Choose separate explicit rates and test sufficient/insufficient credit,
  failed-job release, duplicate terminal, late success and restart recovery on
  disposable PostgreSQL before a bounded real canary.
- Add durable asynchronous submit/status/results and request identity.
  Existing synchronous recovery is not an async submission contract.
- Add request-bound Base USDC x402 payment-before-dispatch and refunds. The
  existing text-only x402 route is not video support.
- Build film manifests, take selection, local editing and agent budgets; update
  Gallery, SDKs and the portable agent skill against the released contract.
- After reviewed merge/deploy and canaries, promote only approved recipes,
  change the default, drain old jobs and retire unused MiniMax H3 paths. Never
  delete model files based only on names or an advertised replacement.

## Verification

The candidate has HTTP, resolver, quoting and dispatch tests with synthetic
dependencies. They assert one graph snapshot, effective duration, correct
distilled reward units, strict settings, seed zero, source fitting and rejection
before quota/upload/reserve/dispatch. The isolated billing tests described above
add actual SQL/worker-terminal lifecycle evidence, not live financial/GPU proof.

Run the focused tests named in `recipes/qualification/AGENTS.md`, plus the full
Grid suite. PostgreSQL/Redis/cross-repo CI remains a release gate; skipped
integration tests must never be counted as passing evidence.

The expanded candidate passed 2039 full local Grid tests / 275 skipped in a
fresh dependency environment with disposable PostgreSQL 14 credit tests. Its
focused money/transport/contract matrix passed 225 / 3 SQLite-only race skips.
The earlier candidate completed required PostgreSQL 16/Redis/cross-repo CI;
the expanded change must pass those required checks again before promotion.
