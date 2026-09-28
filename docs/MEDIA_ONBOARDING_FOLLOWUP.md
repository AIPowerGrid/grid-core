# Media Onboarding Follow-up

## September 28 Review

Worker PRs #26 and #27 are merged. They do not deploy a worker or enable a
generation path. Core PR #211 was deployed as recorded below; advanced media
canaries and worker upgrades remain separate work.

- Fix the omitted-model image default to `FLUX.2 Klein 4B FP8`, matching the
  dispatch name, curated recipe, and price. Explicit selections still win;
  unavailable default capacity does not fall back to another model.
- Minimize `GET /v1/account`: basic read credentials receive generic identity
  hints and no sibling key metadata. The full view requires `account.manage`
  and an interactive native/delegated user or session principal. Existing
  session management and fresh-proof mutation gates remain separate. All
  successful responses are `Cache-Control: no-store`.

The account response keeps the same fields. Console account/key-management
screens must use their existing scoped user session, not substitute a service
or inference credential when the key list is empty. This is response
minimization, not erasure of identity records, an account merge, or a change to
credit/payout ownership.

Read-only production inspection found release `e3741222`, charging `on`, and
the explicit admission list: chat, Responses, Anthropic, single image, video,
image-to-video, and audio. Image-to-image, image batches, video timelines, and
3D were not admitted. No controls were changed by this inspection.

## Core Deployment: September 28

At `2026-09-28T19:00:18Z`, production selected immutable
`ca1f2a127a878ed264589adba177181151da3c5c`, replacing `e3741222`.

- [PR CI](https://github.com/AIPowerGrid/grid-core/actions/runs/36461280688)
  passed 2,168 tests with 12 skips, plus the Redis proof and real PostgreSQL
  Core/Console/node handoff. The
  [exact-main run](https://github.com/AIPowerGrid/grid-core/actions/runs/36462851995)
  passed before deployment.
- Hash-locked binary dependencies installed and passed `pip check`. A fresh
  production backup restored into a generated scratch database and passed
  candidate schema parity at Alembic `0042`; no production migration was needed.
- Preflight confirmed zero running pinned shadow observations. New generation
  and probe ingress was briefly gated, MCP stopped, and work drained before
  Core stopped. Two quiet checks plus the stopped-Core check found no held
  reservations, pending jobs or undelivered stream entries. The historical
  text lag counter was not reset.
- All six workers reconnected before ingress reopened. Public health and
  network status reported the exact release, Redis healthy and the prior
  model set restored. Core and MCP were active with zero automatic restarts.
- A real owned worker credential read `/v1/account` through the public API:
  HTTP 200, `Cache-Control: no-store`, generic linked-identity hints and no
  sibling key metadata. Unauthenticated validator assignments remained 401;
  the retired heartbeat remained 410. No live image was generated in this pass.
- The environment and payout drop-in were byte-for-byte unchanged. Payout and
  backup timer states were preserved. Charging stays on; the same seven paths
  are admitted. No compensation, wallet, model recipe, GPU backend or worker
  runtime was changed.

The payout service had already failed before cutover because the current hour
lacked a reviewed demand reward policy. No transfer retry or policy extension
was authorized or performed by this deployment. This is an outstanding payout
operations issue, not evidence that the media fixes restored payments.

Gorgadon access was verified, but its live workers serve LTX/audio. Image-worker
access was subsequently verified and the isolated GPU checks below executed.
The image host was subsequently upgraded as recorded below; this is not a
fleet-wide upgrade of the separate LTX/audio workers.

## Isolated Image-Worker Evidence: September 28

The owned RTX 5090 image host ran media-worker candidate `cd28ecc` from a
separate staging directory. Its production bridge was stopped only after
ComfyUI was idle and automatically restored afterward; ComfyUI itself was not
restarted. Both services returned active and Core again reported six workers.

The harness exercised the real worker WebSocket registration/job/done/ack
transport against a loopback synthetic coordinator, real ComfyUI, and local
HTTP upload slots. It used no real Grid credential, production credit or reward
ledger, or R2 storage. This is execution evidence, not the billed Grid canary
required below.

- Two-image Klein text-to-image: two distinct 512x512 outputs, approximately
  2.21 seconds warm. Each output's decoded pixels exactly matched a separate
  single-image replay of its declared seed (42 and 43).
- Source-conditioned Klein edit: both installed FP8 and BF16 variants changed
  the red car to blue while retaining the composition, returning 1024x1024
  images in approximately 4.07 and 5.07 seconds respectively. These timings
  are observations on one host, not a fleet guarantee.
- Worker regressions: 215 passed, two skipped. Tests cover independent
  nonconsecutive seeds, exact output/upload counts, unsupported seed layouts,
  and a second-render failure with no uploads. Public gates stayed unchanged.

Two bugs were fixed in [media-worker PR #29](https://github.com/AIPowerGrid/grid-media-worker/pull/29):
the omitted `EmptyFlux2LatentImage` batch binding, and misleading per-image
seeds on native ComfyUI batches. Native batches consume a single RNG stream;
the candidate instead renders each recipe-backed image independently with its
assigned seed and a unique output prefix. This does not promise identical
results across different GPUs, model files, or ComfyUI versions.

The historical image-worker checkout has uncommitted LoRA injection/download
and PNG-to-presigned-format conversion. These were reconciled in PR #30 and
tested before the clean release below replaced the running service. The old
checkout remains intact for rollback; do not reset it or deploy from it.

## Image Worker Deployment: September 28

[PR #30](https://github.com/AIPowerGrid/grid-media-worker/pull/30) merged as
`50476b943dfa1e55ceac5536ccad1e6a4bf17a00`. That exact main commit was selected
on the owned image host at `2026-09-28T22:06:29Z`.

- PR and exact-main CI passed, including dependency/secret checks and manager
  builds. A separate hash-locked Python 3.13 environment on the actual host
  passed 243 tests with two skips; the ComfyUI environment was not changed.
- The loopback GPU harness passed seven cases with the candidate and again
  with its locked runtime: a two-image Klein batch, separate seed-42/43
  replays, source-conditioned edits with each installed precision, Z-image
  baseline, and Z-image with a locally installed LoRA. Batch replays matched
  decoded pixels and uploaded WebP bytes. Every uploaded object was valid
  WebP with the matching receipt SHA-256. The LoRA changed the output; this
  does not certify an adapter's claimed identity or quality.
- The release implements actual image encoding for PNG/WebP/JPEG slots and
  fail-closed recipe-based LoRA injection. Remote adapter downloads require
  operator opt-in, bounded HTTPS downloads, a provider hash and safetensors
  validation. Bearer credentials are restricted to the metadata/download
  origin and are not forwarded to CDN redirects or placed in query strings.
- The service uses a clean detached source tree under
  `/home/aipg/releases/grid-media-worker-50476b943dfa1e55ceac5536ccad1e6a4bf17a00`
  with its own venv. Existing credentials were copied to a mode-0600 private
  config file, not added to the source or unit. Only the image bridge was
  stopped after ComfyUI became idle. ComfyUI's PID did not change.
- Startup fetched and actually rendered the three Core recipes before
  advertising `z-image-turbo`, `FLUX.2 Klein 4B FP8`, and `Krea 2 Turbo`.
  Public Core presence and local status confirmed all three, one image slot,
  no startup error and zero automatic service restarts. Video is no longer
  incorrectly advertised by this image-only host. Other workers were untouched.
- The first cutover was rolled back because the verifier's Python HTTP client
  received a public API 403. The checker now uses the working curl transport;
  the second cutover passed. Removing only
  `30-reviewed-release.conf` from the bridge's systemd drop-in directory,
  reloading systemd and restarting the bridge restored the previous service
  during that rollback. The old source, environment and model files remain.
- Shutdown exposed a non-blocking uncollected WebSocket `ConnectionClosedOK`
  exception in a receive task. Systemd stopped cleanly; record a scoped task
  cleanup regression rather than treating this as a generation failure.

This deployment did not change Core, recipes, model files, charging mode or
public admission. These GPU tests use synthetic dispatch/local upload slots,
not real credit/reward ledgers or R2. Paid end-to-end checks below are still
required before public image-to-image or batch activation.

## Klein Recipe Prerequisite

The curated text-to-image recipe references
`flux-2-klein-4b.safetensors`; the edit recipe references
`flux-2-klein-4b-fp8-backup.safetensors`. A filename difference is not proof of
identical contents or precision. Inspect the actual image worker inventory and
weight digests before selecting the canonical file. Inspection established
that the installed files are NOT equivalent:

| File | Tensor types | SHA-256 |
| --- | --- | --- |
| `flux-2-klein-4b.safetensors` | 149 BF16 tensors | `ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343` |
| `flux-2-klein-4b-fp8-backup.safetensors` | 80 F8_E4M3, 69 BF16, 160 F32 tensors | `97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6` |

The text-to-image model label says FP8 but its current recipe selects the BF16
file. No model label, recipe, or weight was changed by the isolated tests.
Choose and document the intended precision before changing the canonical
filename; do not treat this as a cosmetic rename.

Update the reviewed recipe and its content commitment together where applicable.
Confirm whether the live resolver uses the local recipe or a chain-governed
version; a local edit must not bypass a governed commitment or revocation.

## Canary Acceptance

These remain paid end-to-end acceptance requirements; the isolated GPU checks
above do not fulfill them. Use an isolated staging
coordinator with disposable database/queue state and an explicitly assigned
owned worker, or first implement and review an account-bound, expiring public
path pilot. The existing generation allowlist is global: temporarily adding a
path opens it to every otherwise eligible caller, not just the test account.
Do not turn charging off to get through an admission failure.

1. Pin Core, worker, recipe, model-file digests, and the test-account budget.
   Check worker capacity before borrowing a production GPU; do not interrupt
   another user's render or restart an unrelated model service.
2. Run one source-image edit. Check actual source conditioning, full output,
   canonical job ID, exact recipe, and the appropriate positive price.
3. Run a two-image text-to-image batch. Require two returned objects and two
   distinct output indices/digests, with the expected seeds and dimensions.
   Source-image batches remain rejected until a batch-capable edit recipe exists.
4. Reconcile each job: hold before dispatch, one terminal, one charge, one worker
   ledger row, correct balance delta, and no stale hold. Trace public response
   URLs to the retained result and uploaded objects, not merely HTTP success.
5. Exercise insufficient balance, missing source/invalid parameters, incomplete
   output, worker failure, timeout/recovery, and terminal replay in the isolated
   environment. No-work failures must not earn; refunds and completion must be
   mutually exclusive and idempotent. HTTP timeout alone must not refund a
   still-running job.
6. Verify the gallery exposes editing only for supported models and presents
   every batch output. Preserve existing images and do not publish test output.
7. Record results and explicit rollback before separately activating each public
   path. Keep timeline and 3D outside the initial image rollout.

## Operator Onboarding Work

Extend the existing media-worker wizard instead of adding a second setup app:
select a reviewed model, discover missing nodes/weights, run its local recipe
test, connect, and confirm Core registration. Provide a model-request link with
model/license/workflow/VRAM information; requesting a model does not admit it.

Windows auto-start must preserve loopback access controls, keep credentials out
of task arguments/logs, use explicit executable/working-directory paths, and
support status/disable/uninstall. Test logon/reboot, slow ComfyUI startup, port
conflict, and reconnect. Preserve the uncommitted wizard work already present
in the maintainer's media-worker checkout.
