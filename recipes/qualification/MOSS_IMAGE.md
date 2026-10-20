# MOSS Image Qualification

Status: native-qualified candidate with exact startup copies preparing the
owned-worker rollout and billing canary. Native GPU results and existing-model
regressions pass. Pricing approval and ordinary exact-source release gates
precede controlled activation; a separately bounded Grid billing canary and
explicit public acceptance still precede declaring the release complete.
Inventory alone does not create online worker capacity. Do not recursively
load this directory.

On October20, native qualification on an owned32GiB RTX5090 completes one
1344x768 text image and two1344x768 source-image controls at denoise0.45/0.70,
all50steps/CFG3/Euler/simple/seed739. Observed end-to-end times are38.13,
30.08 and30.08seconds. Each has one native prompt and a fully decoded,
nonblank output. Krea and Klein source-image regressions on the same candidate
also complete (8.06/6.07seconds). Klein's existing source-edit graph emits
1328x768; do not claim those outputs are size-identical or a controlled model
quality ranking. These are single-case timings including varying cache state,
not steady-state throughput or hardware verification.

Visual inspection finds the lower-denoise MOSS output stays very close to the
source;0.70 changes hair/clothing while retaining scene geometry. Neither
uses a separate identity reference or demonstrates portrait fidelity. The
original worker/backend configuration and exact worker identity are restored
after the window, with all three original model claims present. Native tests
use a separate candidate environment with a read-only fallback to the existing
CUDA packages; they do not qualify an independently packaged production
environment. The original TorchCUDA12.8 build runs these controls but the new
runtime warns that optimized operations requireCUDA13.0+. Preserve that
performance limitation rather than claiming optimized kernel coverage.
That initial overlay proof is superseded for runtime independence by a second
native window on a separate103-package, fully wheel-hash-locked environment:
Torch2.14.1+cu130/Torchvision0.29.1+cu130, the same pinned ComfyUI source and
the same model files. Its dependency-lockSHA256 is
`2b67cf2140d256aa09f84a1bc9ca7098c8ad5b65e687b42b7b1f0711f73bd87f`.
All103 upstream package versions have no reported advisories in the audit;
the two official CUDA variant wheels are separately bound by upstream hashes.
This is not a guarantee against unpublished vulnerabilities.

The independent environment passes CPU startup, dependency consistency and six
native cases without replacement submissions: MOSS text36.14seconds,
source-image0.45/0.70 at28.09/28.08seconds, Krea10.06, Klein8.08 and
Z-image10.07. MOSS/Krea/Z-image emit1344x768; Klein retains1328x768.
The original services/configuration are restored after this second window as
well. These single-run cache-dependent timings are not a speedup claim, a
production rollout, or paid Grid evidence. Native input/graph/output bindings,
full decode checks and exact prompt identities stay in private operator evidence.

## Identity And Provenance

- Model ID: `MOSS Image SFT BF16`. Diffusion precision is BF16; the separate
  Qwen3.5-9B text encoder is INT8, not BF16.
- Official model: https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT
- Repack: https://huggingface.co/Comfy-Org/MOSS-Image
- Both model cards identify Apache-2.0. This does not license unrelated Qwen
  Image 2.1 weights; those are not used by these recipes.
- Repack revision: `3284a1dbb351e3396d70f39f2a71cbf6ed2ef1bb`.
- Native ComfyUI support: `e087d8f6cb7c6cdb11b59dc7c9bf17c79f8d877d`,
  https://github.com/Comfy-Org/ComfyUI/pull/17095.

| Component | File | SHA-256 |
| --- | --- | --- |
| Backbone | `moss_image_sft_bf16.safetensors` | `f96738d8116d78b7316b57c663d8c98591ddafab7488f48e3099b7655e34ab6f` |
| Encoder | `qwen3.5_9b_int8_convrot.safetensors` | `32e3787472f708fa5f83624acbf5ef23f7fc89c439e05065ea57cfe5a97f025a` |
| VAE | `flux2-vae.safetensors` | `868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3` |

## Workflow Contract

The fixed graph follows the native MOSS loader/conditioning path, without an
external prompt enhancer. Defaults are 50 steps, CFG 3, Euler/simple; MOSS's
native sampling configuration supplies shift 5. The faster upstream Comfy
template uses a different 25-step/CFG-2.5/er_sde tradeoff. Do not describe these
graphs as the exact default template or silently change schedules after proof.

Text generation uses EmptyFlux2LatentImage. Img2img instead loads, resizes and
VAE-encodes one source image, then denoises it at 0.2-0.8 (default 0.65).
There is no separate portrait, mask, identity embedding or trained edit
conditioner. Describe the desired final scene; preservation and character
similarity must be judged from examples, not promised by the model label.

Candidates expose only prompt/negative prompt, seed, dimensions, bounded
steps/CFG, fixed sampler/scheduler and (for img2img) source/denoise. There is
no client checkpoint, precision, arbitrary graph, LoRA or hosted LLM selector.
Native examples do not establish paid Grid dispatch or universal GPU fit.

## Promotion Gates

1. Hash-verify the three files and pin a compatible runtime outside serving
   paths. Keep the old runtime and environment intact for rollback.
2. Withdraw the owned image worker only after an idle/zero-held check; repeat
   that check after withdrawal. Never overlap public work and native tests.
3. Render text and source-image controls; decode complete outputs and inspect
   them visually. Record exact graphs, seeds, dimensions, timings and hashes
   privately. Never commit user prompts/media or signed URLs.
4. Verify Krea, Klein and every other advertised model on the candidate;
   restore the original worker if any required capability regresses.
5. Select an explicit tariff and model-specific API defaults. Test quotes,
   reserve/settle/refund behavior, recipe selection and worker preflight.
6. Complete ordinary Core release gates before controlled activation. Verify a
   freshly approved, capped real Grid canary before declaring public acceptance.
   A local render is not a production deployment or permission to spend a user's
   credits. Retain the old worker configuration for immediate withdrawal/rollback.
