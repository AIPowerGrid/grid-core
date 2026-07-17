# Governed ACE-Step Audio Controls

Status: planned control-surface upgrade. The live Grid currently serves only
the signed `ace-step-v1.5-text2music` recipe. This document does not claim that
the controls below are live until the Core recipe, signed worker profile, and
tests ship together.

## Objective

Expose controls that musicians can understand and that materially constrain a
XL Turbo generation, while preserving one reproducible, billable job contract.
Do not proxy the upstream ACE-Step API wholesale.

The XL managed runtime exposes `acestep-v15-xl-turbo` only. Its pinned API
recommends eight inference steps for XL Turbo. Therefore the current public
default remains eight steps; a larger value is not advertised as a quality
improvement without benchmark evidence.

## Public Text-To-Music V1.1

The next governed text-to-music recipe will accept these request controls:

| Control | Validation | Default | Why it is public |
| --- | --- | --- | --- |
| `prompt` | 1-2,000 chars | required | Musical intent and arrangement. |
| `lyrics` | 0-20,000 chars | empty | User-authored vocal content. |
| `seconds` | 10-300 | 30 | Deterministic price and den unit. |
| `seed` | 0 through `2^53-1` | Core-randomized | Reproducibility without trusting a worker RNG. |
| `inference_steps` | 1-20 | 8 | XL Turbo-supported experiment knob; UI labels eight as recommended. |
| `bpm` | integer 30-300 | unset | Direct musical tempo constraint. |
| `key_scale` | normalized musical key | unset | Direct harmonic constraint. |
| `time_signature` | 2/4, 3/4, 4/4, 6/8 | unset | Direct rhythmic constraint. |
| `vocal_language` | normalized ISO 639-1 code or unset | unset | Constrains vocal generation when lyrics exist. |

The app should provide BPM, key, signature, and language as ordinary controls,
not require users to encode all of them in the caption. Presets may populate
these fields, but never silently rewrite a submitted prompt or lyrics.

## Explicitly Not Public Yet

| Upstream capability | Reason it is not a public knob today |
| --- | --- |
| `guidance_scale`, `shift`, ADG, CFG intervals | Non-Turbo controls or not qualified on the active XL Turbo profile. |
| Custom `timesteps` and `infer_method` | Raw sampler configuration destroys a stable, comparable recipe surface. |
| `batch_size` | One output per job keeps quotas, pricing, receipts, and worker settlement exact. Variations should be separate jobs. |
| `use_random_seed` | Core owns seed normalization and receipt reproducibility. |
| LM temperature, top-k/top-p, backend, CFG | Runtime-operation knobs, not customer creative controls. |
| `sample_mode` | Produces opaque LM-authored prompt/lyrics and weakens user-intent provenance. |
| `audio_code_string` | An expert/internal transport requiring a separate validation and storage policy. |
| Base/SFT model selection | Not loaded or qualified by the canonical XL Turbo worker profile. |
| Output format selection | Core stores one verified WAV output; conversion belongs at an edge/export layer. |

## Prompt Assist Is an Experiment, Not a Default

ACE-Step can use its 5 Hz language model to format a caption or plan audio
codes (`use_format`, `use_cot_caption`, `thinking`). Those modes change the
effective input and can change latency, VRAM use, and output character.

Before any public "Enhance prompt" or "Quality" mode, the Grid must:

1. Create a distinct recipe root with the exact fixed LM behavior.
2. Preserve the user input and commit the normalized effective input in the
   job receipt/metadata without logging either in service journals.
3. Benchmark it on the qualified 3090 profile at eight steps before publishing a new recipe root.
4. Publish only a named, measured mode. Do not expose LM sampler internals.

The benchmark set must contain instrumental and vocal briefs, explicit and
implicit tempo/key requests, 10/30/60-second runs, and at least three fixed
seeds per configuration. Record latency, peak VRAM, output duration, silence
or clipping checks, tempo/key adherence where measurable, and blind human
ratings for harmony, coherence, and prompt adherence. A mode ships only when
it is no worse on reliability and wins clearly enough on the human evaluation
to justify its cost.

## Audio-Conditioned Modes

Reference audio, cover/remix, repaint, and continuation are separate recipes,
not optional text-to-music fields. They require a source-audio ingestion
contract before exposure:

1. The browser uploads to a short-lived, account-scoped presigned object URL.
2. Core validates media type, decodability, duration, channel count, and size;
   it stores a content hash and does not accept a worker-local file path.
3. The job binds the source hash, mode, edit interval, and influence strength
   into its recipe commitment and signed receipt.
4. A selected worker gets only a short-lived read URL and deletes its local
   material after the job. Source audio stays private by default.
5. The product presents an ownership/authorization confirmation. No blanket
   claim that source-audio transformations are safe to publish follows from
   technical capability alone.

Reference-audio style guidance is the first candidate after V1.1. Remix,
repaint, and continuation follow only once this ingestion and provenance path
is tested.

## Coordinated Release Gate

Adding even a safe public control changes the canonical recipe root. The
following artifacts must land and be verified as one release:

1. `grid_api/services/audio.py`: canonical recipe limits and variables.
2. `grid_api/routers/audio.py`: typed request validation and governed payload.
3. `bridge/audio_runtime.py`: local bounds checks and exact forwarding to the
   loopback ACE-Step API.
4. `bridge/profiles/ace-step-v1.profile.json`: matching recipe root, followed
   by offline release signing and managed-worker deployment.
5. Core router/worker contract tests and worker canary/qualification evidence.
6. `aipg.music`: matching user controls, presets, and the honest XL Turbo default.

Do not deploy Core alone or edit a production profile in place. An old profile
must remain accepted until the new signed profile is active and the managed
worker has passed its canary.
