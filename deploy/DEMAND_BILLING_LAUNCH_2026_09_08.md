# Demand billing launch evidence - 2026-09-08

## Posture

IN PROGRESS. Not a global billing activation or permission to resume payouts.
The goal covers every public generation path and all first-party frontends.
Unverified paths must be disabled or fail closed before public charging launch.
Historical accrual and disputed payments are outside this rollout.

## Reviewed deployment

- Core PR #132 initially deployed as `a754b6898b4a6111758b104ee633de18ef0fe252`.
  The coordinated worker-release task subsequently deployed combined commit
  `0b4630c555ac10be6a64b5610b493ca38c0730ac`, including service-budget PR #134.
  Independently re-read the selected production checkout to confirm that SHA.
  The deploying task reports fresh backup/restore at Alembic `0039`, unchanged
  environment/services, and all 14 pre-cutover worker connections recovered.
  Retained `a754b689` is the immediate rollback. This billing task must not
  perform a competing Core restart during worker qualification.
- Required CI passed with 1,399 tests passed and 9 skipped, PostgreSQL 16
  backup/restore proof, migration/schema parity, and secret/infrastructure scans.
  CodeQL also passed. Skipped cases and live canaries remain separate evidence.
- Candidate installed only the reviewed hash-locked binary wheels and passed
  `pip check`. No dependency lock, Alembic, systemd, or Nginx changes between
  the prior live release and this candidate; existing service/proxy assets stayed
  unchanged.
- Production backup restored into a disposable database using the exact new
  candidate: Alembic `0039`, no new upgrade operations, proof passed. This did
  not restore over or alter the live economic database.
- Configuration preflight and the restarted process confirmed charging still
  `allowlist`, one account, zero service cohort entries, `z-image-turbo` model
  cohort, and no `WORKER_REWARDS_PAID_ONLY_SINCE` value. The production environment
  file was byte-for-byte unchanged across cutover. The new economic policy is
  deployed but NOT activated.
- Verified the running process directory and both local/public health commit.
  Redis healthy; all 13 previously connected workers recovered after restart.
  Worker payout timer and service remain inactive; no payment or backpay ran.
- Music PR #3 merged and deployed as `b9713d996eeba87dee9ea1cbebb7948cebdc7141`.
  Container build/auth smoke and post-restart HTTP checks passed, with the
  signed-session delegation comparison active. Music PR #4 records that release.
  No billing flags or balances changed and no real audio generation ran.

## Verified containment

- Production `aipg-payout.timer` stopped and disabled; the payout one-shot was
  inactive with no in-flight process when paused. Rechecked inactive/disabled
  after the local implementation pass. Grid API remains active.
- Original environment preserved in the root-only rollout directory on the
  production host. Do not publish that file, credentials, or raw customer IDs.
- Fresh backup `grid-postgres-20260908T193201Z.dump` passed checksum verification
  and a full disposable-database restore with Alembic `0039` and no schema drift.
  Backup/restore used the deployed `84fe0fd6` release. Neither operation restored
  over the live database or changed historical payment records.
- Repaired only missing group access on the selected release root, `scripts/`,
  and the versioned backup script. The hardened backup service could not traverse
  their `0700` permissions. Future deployments must verify these exact paths.
- Last inspected runtime was `GRID_CHARGING_MODE=allowlist`, one account,
  `z-image-turbo` model cohort, and zero direct-service allowlist entries.
  Bounded all-model service exceptions require their own inventory; these counts
  alone do not prove that all other traffic is free or charged.

## Local implementation and evidence

Implementation branch: `fix/demand-billing-launch`, based on `61a9ffb9`.
The reviewed deployment above supersedes the initial local-only posture.

- Invalid explicit charging modes now reject startup before dependencies/loops
  and reject authorization, instead of silently falling back to free inference.
- Added real-Postgres text/media duplicate-terminal and terminal-versus-refund
  races. Only a committed terminal may leave both a worker ledger entry and a
  charge; refund-winning races cannot leave a payable completion.
- Added prospective purchased-fraction reward aggregation for account, wallet,
  and diagnostic consumers. Legacy rows remain unchanged. New unreserved,
  free-only, promo-only, held, refunded, malformed, and unknown-source work is
  ineligible for the unrestricted pool. x402 retains its external-settlement gate.
- Added a network-wide 0.5% SmolLM-family custodial emission cap with no
  redistribution, including account splitting and walletless accrual tests.
- Full Core suite before the reward-policy additions: 1,124 passed, 230 skipped.
  The configured credit and payout concurrency modules ran against isolated
  PostgreSQL 14.19. Other skipped external integrations are NOT verified.
- Reward eligibility, cap, and x402 focused suite: 71 passed, including both
  SQLite with foreign keys and real PostgreSQL reward queries. Full candidate
  suite after final UUID lookup compatibility checks: 1,178 passed, 230 skipped.
  Matching production-version PostgreSQL CI and live canaries still required.
- Reservation correlation uses indexed primary-key comparisons for canonical
  and compact UUID spellings, not per-row functions on the reservation key.
- Source review: Music and Gallery send delegated user tokens for generation
  and preflight quotes. Gallery checks the refreshed identity against its signed
  session. Music lacked that session comparison; a separate candidate branch
  `fix/music-billing-identity` added it with no-dispatch/no-credit-read tests and
  has now deployed. Gallery's currently deployed flow still needs live verification.

## Service and Chat image follow-up

- A fresh read-only production inventory found nine active service clients,
  including four keys with direct-inference permission. Every current direct
  service has positive per-request and daily caps. The uncapped MCP bridge has
  no direct-inference permission; its delegated users still require their own
  credits when charging is selected. No keys, caps, or balances were changed.
- Core PR #134 merged as `c40232b2`: runtime service-budget validation now
  rejects missing/malformed direct-service caps rather than relying only on
  provisioning checks. Zero no longer means unlimited in the Redis guard.
  Configured delegated-app caps are validated, but omitted app-wide ceilings
  remain compatible with user-owned charging. It is now included in the
  combined `0b4630c5` production release above; no spending caps changed.
- Focused Core auth/billing/router checks: 377 passed, 41 skipped. Required
  PR CI also passed with PostgreSQL 16 restore/schema and money-path checks;
  skipped and mocked cases are not production canary evidence.
- Chat's running image-tool, constructor, and identity-module hashes match
  the reviewed pre-fix source. Text uses delegated identity; the image tool
  does not. Its current database provider uses an older internal endpoint and
  a different key from the canonical Chat service. A read-only Core credits
  request with that image key succeeds with `charging_enabled=false` and
  `service_budget=null`. Do not treat Chat text wiring as image billing proof.
- Chat PR #1 (`fix/chat-image-billing`) adds a per-image lazy user-token factory
  and requires the canonical Chat service endpoint/key for that delegation.
  Local identity/model-sync/image-tool/provider checks: 44 passed, with Ruff
  and targeted type checks passed. PR review/CI, provider configuration migration,
  deployment, and a real user-attributed image canary are still required.
  No production image-provider record or credential was changed during audit.
- Chat's inherited Python CI requested unavailable upstream private runners.
  A fork-specific hosted-runner adjustment preserves the full test commands and
  hash-verified dependency install. On commit `dab173b24d`, unit CI ran:
  4,989 passed, 30 skipped. Full type CI exposed fixture-type errors; corrections
  are pushed in `3877c22c38` with 49 focused tests and full local type checking
  passed, awaiting fresh CI. Other inherited infrastructure-dependent workflows
  remain unverified; none of these results proves a live Chat deployment.

## Signed-in Gallery canary

- The user signed in normally. No synthetic session, credit grant, funding
  transfer, or charging configuration change was used for this canary.
- Krea remained preview-only for this cohort. Selecting Z-Image Turbo changed
  the UI to an estimated paid charge of USD 0.003. The pre-canary displayed
  purchased balance was USD 0.0037 (rounded).
- Generated one private 1024-square image on 2026-09-08 at 20:57 UTC, Grid job
  `8f01d616-01d1-4d5e-b6f0-d9797f54a214`, through the real Gallery UI.
  The UI returned the image with an 8.9-second generation time.
- Read-only production SQL confirms one settled `aipg-art` reservation:
  reserved/actual 3,000 micro-USD, zero free/promo allocation, source `credits`.
  Exactly one matching-account credit entry is -3,000 (`reserve:image`), and
  exactly one completion records one image and 0.32 raw den. No refund is due
  for this exact-priced success. Remaining purchased balance is 675 micro-USD.
- A second UI submission returned insufficient Grid credits and no new image.
  This verifies the visible rejection, not by itself a full queue-level
  no-dispatch proof. Automated backend rejection tests cover that boundary.
- Gallery PR #20 merged as `5836668b91a25cb2a1491af1fca6d8b1fa6d1cb6`
  after all backend/frontend/browser/security/CodeQL checks passed. It also
  verifies that Core credit/quote response IDs match the delegated account
  before generation. Production candidate is being built, not yet activated.
- This proves one purchased-credit Gallery image path only. Cross-site balance
  parity, other modalities, refund/crash canaries, global charging, and prospective
  worker reward activation remain incomplete. Raw den is not evidence that the
  still-unset purchased-only reward cutoff has been activated.

## Remaining launch checklist

- [x] Review and merge candidate; record exact release SHA and CI evidence.
- [ ] Review query performance and immutable prospective cutoff selection.
- [ ] Reconcile requested emission budget, no overlapping payout periods, and
      every payout entrypoint before restarting any sender. No treasury refill.
- [ ] Verify credits and free/promo ceilings in actual production processes.
- [ ] Complete request-to-terminal inventory below, including negative tests.
- [ ] Prove Google-only, wallet-linked, zero-balance, free-exhausted, and capped
      direct-service cases; same canonical balance across first-party apps.
- [ ] Run real funded canaries, including streaming and multistage Director;
      reconcile reservation/debit/refund/reward eligibility for each job.
- [ ] Verify funding retry persists receipt without another transfer.
- [ ] Verify billing/orphan/reward/treasury alerts and operational response.
- [x] Deploy reviewed Core/Music code with existing configuration preserved.
- [ ] Verify remaining frontend releases and final activation configuration.
- [ ] Migrate Chat image provider to canonical service credentials only with
      the delegated-image release; verify the charged account and retain a
      protected rollback record. Inventory old-key consumers before revocation.
- [ ] Enable only verified public paths; record disabled paths and rollback.
- [ ] Resume payouts only after prospective policy reconciliation passes.

## Entry-point inventory

Source wiring is not live proof. Each row still needs exact account/service
attribution, reserve-before-dispatch, terminal/refund, and rejection evidence.

| Path | Core ownership / shared billing path | Live canary status |
| --- | --- | --- |
| Chat completions, including media shim | `routers/openai.py`, credits or media service | Pending |
| Responses | `routers/responses.py`, `_passthrough.py` | Pending |
| Anthropic messages | `routers/anthropic.py`, `_passthrough.py` | Pending |
| Image and image-to-image | `routers/images.py`, `services/media.py` | Paid Gallery Z-Image success verified; img2img pending |
| Video and image-to-video | `routers/videos.py`, `services/media.py` | Pending |
| Audio | `routers/audio.py`, `services/media.py` | Pending |
| 3D | `routers/threed.py`, `services/media.py` | Pending or disable |
| Batch images | Per-item media holds; frontend fan-out must be traced | Pending |
| Director first frame / segments / retries | Gallery orchestration into image/video routes | Pending |
| Direct API, SDKs, provider integrations | Same public routes; verify no alternate dispatch bypass | Pending |
| Chat, Art, Music, Console | Delegated identity and shared purchased balance | Pending |
| Bots and direct service accounts | Explicit service identity and request/day ceilings | Pending |
| x402 | Independent external payment proof; keep dark unless verified | Pending or remain dark |
| Validator/worker setup probes | Bounded internal work; prove no ordinary emission entry | Pending |

## Rollback boundary

Do not delete holds, ledger rows, payout rows, or nonce history. Preserve the
reward cutoff once activated. Stop generation on failing paths rather than
silently reopening unrestricted free traffic. Disabling charging only stops new
holds; existing holds still need settlement/release and monitoring. Keep payout
timers paused until reconciliation, even if demand charging is rolled back.
