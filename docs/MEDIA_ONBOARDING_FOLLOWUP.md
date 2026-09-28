# Media Onboarding Follow-up

## September 28 Review

Worker PRs #26 and #27 are merged. They do not deploy a worker or enable a
generation path. The Core changes in this follow-up are candidates until an
exact release passes CI and the deployment procedure.

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

## Klein Recipe Prerequisite

The curated text-to-image recipe references
`flux-2-klein-4b.safetensors`; the edit recipe references
`flux-2-klein-4b-fp8-backup.safetensors`. A filename difference is not proof of
identical contents or precision. Inspect the actual image worker inventory and
weight digests before selecting the canonical file. Do not rename the recipe
blindly or claim the edit graph works because text-to-image works.

Update the reviewed recipe and its content commitment together where applicable.
Confirm whether the live resolver uses the local recipe or a chain-governed
version; a local edit must not bypass a governed commitment or revocation.

## Canary Acceptance

These are requirements, not executed test results. Use an isolated staging
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
