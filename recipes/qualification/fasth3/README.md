# FastH3 Omni Candidate

This directory prepares an offline Core compiler, not a live Grid model. There
is no public API support, tariff, worker registration or activation switch in
this change. The current live recipe parser rejects the separate H3 schema;
startup does not recurse into qualification directories.

## Components And Rights

The JSON records immutable model/runtime revisions and component digests for
the official ComfyUI-native NVFP4 QAD release. It does not contain weights.
These are distinct from FastVideo's own checkpoint format and inference kernels.
No dependency, driver, installed runtime or existing LTX recipe changes here.

The model card selects the
[MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE),
which excludes US/EU/UK/South Korea, including hosted use. Separate applicable
authorization has not been supplied. Further model execution and public serving
remain gated. Source-code licensing does not grant model-use rights.

## Candidate Contract

`grid_api.services.fasth3_contract.compile_candidate` takes strict compiler
inputs, not public API requests. It returns the Core-owned graph, normalized
execution units and explicit unbound source roles. Filenames in the graph are
placeholders, not proof of validated, uploaded or authorized source bytes.

- Fixed eight-step res_multistep/simple sampling, guidance1, shifts12/3,24fps.
- Explicit124..362frames on17k+5; invalid lengths reject rather than round.
- Candidate canvases1344x768,768x1344,768x768. These bounds are not GPU fit or
  quality qualification. The full range must not be advertised automatically.
- Separate `appearance_image` and `opening_image` presence flags.
- `audio_mode=generated|reference|preserve`, never ambiguous reference audio.
  Both audio-input modes require an explicit image for this initial scope.
- Appearance/reference audio condition generated output through reference
  slots; they do not lock frame zero or promise the original waveform.
- Opening images and preserved soundtracks select explicit guide experiments.
  The preserved soundtrack is also selected for final mux. This is graph wiring,
  not native compatibility, timing, identity or lip-sync acceptance.
- No client filenames, graphs, video references, LoRAs, precision, sampler or
  schedule overrides. Unknown fields and absent reference labels reject.

`quote_units` computes an integer micro-USD ceiling from exact frames/24 and a
supplied positive server rate. Its test rates are synthetic. It neither selects
a tariff nor moves money. Future quotes/reservations must bind one approved,
versioned rate and the exact same execution duration.

## Offline Source Preparation

`fasth3_sources.prepare_candidate` additionally checks all expected inline roles
and byte limits before returning validated sources. Each image/audio input has
a separate SHA-256, byte count and role commitment; nothing is uploaded.

Images must fully decode as unrotated, nonanimated, opaque PNGs,32..2048pixels
per dimension. Opening images must already match the candidate output canvas;
appearance images keep their own geometry. Nonopaque images require an explicit
flattening/preview step upstream, never silent alpha removal. Sources are not
reencoded, auto-cropped, fitted or otherwise changed after commitment.

Audio inputs are canonical metadata-free48kHz stereo PCM16 WAV. Voice/music
references are2..15seconds independently of target duration. Preserved sound
must contain exactly frames*2000samples; short/long audio rejects rather than
padding or trimming. Output expectations distinguish generated32kHz audio from
the explicitly muxed48kHz soundtrack. Valid waveform transport is not lip-sync
or identity proof.

This remains a disabled preparation helper, not a public upload route. Account
ownership, private-bucket access, source retention/cleanup, worker revalidation,
process-isolated decoder qualification and purchased end-to-end proofs are still
required before activation. Keep the legacy LTX validator unchanged.

## Remaining Release Gates

Resolve model-use rights and required safeguards. Qualify native text, opening,
appearance and audio combinations, including clocks, scene/identity quality,
lip sync, capacity, cold/warm timing and matched LTX controls. Qualify the H3
source validator/commitments and add a worker-feature contract; never reinterpret
LTX's8n+1frame,48kHz output or Ingredients-only image assumptions globally.

Then integrate disabled-by-default recipe/API admission, authoritative pricing,
private uploads/ownership/retention, output verification and Gallery UI. Reuse
the existing durable reservation, execution, settlement, refund and GET recovery
authorities. Require isolated lifecycle tests, bounded purchased canaries,
reviewed merges, exact release checks, retained recovery and tested rollback
before a one-worker public canary. Preserve available LTX capacity throughout.

## Verification

The H3 unit suite checks strict units, all candidate source combinations,
absent live capability/price, public-async denial and unpriced billing denial
before database access. Existing LTX contracts are unchanged. Tests use no
model weights, live workers, storage credentials or purchased credits.
