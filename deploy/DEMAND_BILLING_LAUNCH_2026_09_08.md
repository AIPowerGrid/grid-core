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
  passed. Subsequent hosted unit and full type CI passed; commit `d490442316`
  also passed the real PostgreSQL migration suite (15 passed, 1 skipped).
  Other inherited infrastructure-dependent workflows
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
  before generation. That release is now active after the production Next build,
  production-only lockfile reinstall, Go race tests/vet/build, and health checks.
  Both services are active; the running backend binary hash matches the candidate.
  Environment, Nginx, and service definitions stayed unchanged, with `2bc9c7e6`
  retained for rollback. A fresh signed-in Google page load retained the session,
  loaded the stored canary image, and showed the same paid balance and Z-Image quote.
- This proves one purchased-credit Gallery image path only. Cross-site balance
  parity, other modalities, refund/crash canaries, global charging, and prospective
  worker reward activation remain incomplete. Raw den is not evidence that the
  still-unset purchased-only reward cutoff has been activated.

## Admission candidate and coordinated release state

- Core runtime advanced to `d139835324fe9b05820b1ada1984fea0bb9fb538`
  through the coordinated worker-setup release. The subsequent `976db957`
  main commit is documentation-only. Do not deploy an older billing checkout
  over those fixes.
- PR #138 includes the billing evidence previously proposed in PR #135 and
  adds `GENERATION_ENABLED_PATHS`, an explicit public generation allowlist.
  Omission preserves existing paths for a compatible dark deployment; an
  empty JSON array rejects all new public generation. This is independent of
  charging cohorts and has not been activated in production.
- Text, passthrough, and shared media admission reject disabled paths before
  reservation/dispatch. Image batches, image-to-image, image-to-video, and
  video timelines require both their base modality and extra capability.
  The Chat media shim uses the same media admission check. Listings and quotes
  are not admission guarantees. Existing jobs retain their terminal processing.
- Local verification: 44 focused admission tests passed. The full Grid suite
  before the final media-shim regression passed 1,225 with 264 skips. Required
  PostgreSQL 16 CI on the final combined candidate remains a merge gate;
  local tests and older-head CI are not substitutes.
- Worker setup staging also recorded two ordinary fixture jobs (4.58 raw den,
  no reservations). The fixture was stopped, its key revoked, and its payout
  address cleared after confirming no payout records. Preserve those rows and
  use the protected staging audit for explicit historical payout exclusion:
  removing the wallet does not remove denominator weight, and a prospective
  reward cutoff cannot retroactively exclude these jobs.

## Combined admission and monitor deployment

- PR #138 merged as `91e19739`; PR #139 then merged as
  `adc9a21ec304fb7c61c536c4417711f284c6fc60`. The latter is now the
  immutable production release (cutover verified 2026-09-08 at 21:50:39 UTC),
  retaining the worker setup fixes from `d1398353`.
- Required PR #139 CI passed on PostgreSQL 16: 1,486 Grid tests passed with
  9 skips, backup/restore and migration parity passed, and the real
  Core/Console/node handoff passed. CodeQL and secret/infra scans passed.
  Local focused verification additionally passed 41 billing/alert tests,
  including real isolated PostgreSQL 14 concurrency; that scratch server was
  stopped after testing.
- The monitor now reconciles each purchased-credit account in one SQL
  statement. Equal-and-opposite account discrepancies no longer cancel out,
  and concurrent commits cannot split its reads across snapshots. Production
  read-only preflight found six accounts, zero mismatches, zero negative
  balances, and zero net drift. The equivalent complete query took 0.422 ms
  at current size; future ledger growth still needs operational monitoring.
- Before cutover, the candidate installed the unchanged hash-locked binary
  dependencies and passed `pip check`. A fresh 119 MB Grid-schema backup was
  restored into a generated scratch database and checked against the candidate.
  Live Alembic parity remained `0039`; no production migration was needed.
  Protected evidence is under
  `/var/lib/aipg-backup/demand-release-adc9a21e/`, including the snapshot,
  checksum, restore/drift proof, configuration backup, and timer comparisons.
- Public health reports the exact new SHA and Redis healthy. The supervisor
  and child processes use the new immutable working directory; billing source,
  admission source, and config hashes match the candidate. The API is active
  with zero automatic restarts, and anonymous assignments still return 401.
  Worker reconnection was checked separately from API health: all 12 workers
  online before restart returned. The last, serving `qwen3:1.7b`, reconnected at
  21:54:10 UTC; public health then showed the complete pre-restart model list.
- Configuration, Nginx, unit definitions, and timer states were preserved.
  Process environment confirms `GRID_CHARGING_MODE=allowlist`, legacy charging
  flag 0, daily-free spending 0, promo global gate 1, and both the generation
  admission allowlist and prospective reward cutoff unset. Promo spending
  remains subject to its separate campaign allowlist; the global gate alone
  does not prove a funded grant. One static, non-economic rollout test alert
  was queued and accepted by the configured Discord transport using the deployed
  alert module. This proves HTTP delivery, not that a human saw it or that every
  billing fault triggers the right alert. Payout timer and sender remain inactive.
- Rollback target is `grid-core-d1398353`, with the compatible `0039` schema
  retained. This was a code deployment, not global billing activation: new
  admission restrictions and the reward cutoff still require deliberate setup.
- Chat packaging at `ced82ded459f646d1742374196599403e4510361` passed hosted
  Linux/amd64 Dockerfile build, packaged-source hash comparison, and offline
  imports as UID 1001 (run `34281434136`). That image is CI build evidence,
  not a published production artifact or a Chat deployment. Its other inherited
  infrastructure-dependent checks remain unverified.

## Read-only reward-policy simulation

- A standalone read-only process applied the proposed paid-only rule in memory
  to the completed 2026-09-08 20:00-21:00 UTC window. No setting, reservation,
  credit, payout, or historical ledger record was modified. This is a
  counterfactual, not a revised allocation or authorization to pay that hour.
- The unchanged policy selected 3,822.43 raw DEN across six accounts. The
  simulated paid-only rule selected 0.32 DEN for one account, matching the
  purchased Gallery canary. This is evidence that the eligibility filter
  excludes the unbilled work in this sample, not a complete fraud-detection proof.
- The current runtime hourly budget is 208.33 AIPG. The allocator still awards
  the whole budget when one non-SmolLM job is eligible: the USD 0.003 image would
  receive 208.33 AIPG under this simulation. No token/USD valuation is implied.
  The SmolLM-family cap does not limit other models' low-demand windfalls.
- Payout resumption therefore needs an explicit decision about subsidy
  intensity, not just a billing flag: either deliberately approve a fixed
  bootstrap pool at low demand or introduce a reviewed per-work/revenue-linked
  emission ceiling with clipped allocation left unspent. Do not invent a
  token valuation or silently introduce that economic policy during deployment.
  Keep the payout timer paused until this and historical exclusions are reviewed.

## Monitoring follow-up (not production activation)

- Treasury monitoring merged through PR 141 as
  `1242ab8b118c9fe5f66b0a207477ff8f1ad76ea6`. Required hosted tests passed:
  1,511 Grid tests with 9 skips, PostgreSQL 16 restore/schema proof, 15
  anti-gaming tests, and the Core/Console/node handoff. CodeQL and secret gates
  also passed. The running Core remains `adc9a21e`; this merge did not deploy
  or configure treasury warnings.
- The reward-backing monitor is a separate default-off candidate. It observes
  the last complete UTC hour using the allocator's current prospective boundary
  and shared purchased fraction. It includes walletless accrual and warns about
  legacy unbacked eligibility, including mixed free/promotional shares, without
  claiming a transfer occurred. Unfunded x402 and work already excluded by the
  prospective rule do not trigger false reward-exposure alerts.
- Candidate verification: 72 focused tests passed across SQLite and an isolated
  local PostgreSQL 14 cluster, including unchanged historical DEN and boundary
  behavior. The scratch cluster was stopped afterward; production was not used
  for these tests. Full local Grid suite: 1,270 passed, 277 environment-dependent
  skips. Hosted PostgreSQL 16 CI and production query-performance/alert proofs
  remain required. A clean recent-hour report does not reconcile historical
  backpay or fix the low-demand fixed-pool economics described above.

## Monitor release deployed, activation still separate

- PR 142 merged as `4fa8bb651ed6e7f04c6adcdeca97ca81c91391c7` with
  a byte-identical tree to its tested head. Required run `34285211188` passed
  1,538 Grid tests with 9 skips, 15 anti-gaming tests, the PostgreSQL 16
  backup/restore/schema gates, and the real Core/Console/node handoff.
  CodeQL and secret gates passed.
- A standalone read-only production candidate query for 2026-09-08
  21:00-22:00 UTC completed in 0.0808 seconds, reporting 263 jobs and
  3,625.38 DEN without full purchased backing under the existing unset cutoff.
  This is unrestricted-pool eligibility exposure, not evidence of transfers.
  The process used read-only transactions and a 15-second statement timeout.
- The treasury candidate read both balances at one Base block against the
  configured payout signer address and token; token decimals were verified as
  18. Gas balance was approximately 0.00723 ETH and AIPG balance approximately
  0.00284. Hypothetical thresholds of 0.001 ETH and 5,000 AIPG correctly
  classified only AIPG as low. No threshold was persisted and no refill occurred.
- Immutable release `grid-core-4fa8bb65` became current at
  2026-09-08T22:27:04Z. A fresh protected database backup was restored and
  tested against the candidate before cutover. Evidence is under
  `/var/lib/aipg-backup/demand-release-4fa8bb65/`. Live Alembic remains `0039`;
  no production migration was needed. Rollback is `grid-core-adc9a21e` with
  that compatible schema retained.
- Public health and supervisor/child working directories report the exact
  release. API is active with zero supervisor restarts. Config, Nginx, systemd
  units, and timer state were preserved. Payout timer and sender are inactive.
  Charging remains allowlisted, admission restrictions and the prospective
  reward cutoff remain unset, and both new monitor flags remain unset/off.
  This deployment is not global charging or monitor activation.
- Post-deploy read-only reconciliation found six purchased-credit accounts,
  zero mismatches, zero negative balances, and zero aggregate drift. Equivalent
  health SQL executed in 0.302 ms at the current small dataset size; this is
  not a future-scale performance guarantee.
- All 12 pre-restart workers and 16 advertised models were online again by
  2026-09-08T22:28:55Z, including the delayed Qwen 8B reconnect. This is fleet
  recovery evidence, not a paid generation canary for each model.

## Read-only monitors activated

- At 2026-09-08T22:33:37Z, enabled `GRID_REWARD_MONITOR_ENABLED` and
  `GRID_TREASURY_MONITOR_ENABLED` on the same immutable `4fa8bb65` release.
  Treasury warnings use the verified payout signer address and token with
  thresholds of 0.001 ETH and 5,000 AIPG (18-decimal raw units).
- The preflight validated the candidate settings before activation. Exactly
  six monitoring keys changed; all other parsed environment values were
  identical. The protected snapshot and activation proof are in
  `/var/lib/aipg-backup/monitor-activation-4fa8bb65/`. Supervisor and child
  environments subsequently showed both flags set to `1`, with charging still
  `allowlist` and the reward cutoff still unset.
- A standalone invocation of the deployed checks, using read-only SQL and the
  production RPC, exercised both expected warnings. Discord transport accepted
  `treasury_low_aipg` and `unbacked_reward_eligibility`; the two extra events were
  marked `delivery_canary` and used separate dedupe keys. This proves the real
  check-to-transport path, not human receipt or a completed operator response.
  Normal background warnings retain the existing cross-process dedupe window.
- Core remained healthy and all 12 workers recovered. No credit, reservation,
  historical DEN, reward policy, or payout state was changed by activation.
  Both the payout timer and sender remain inactive.
- To roll back only these warnings, disable their two flags. Never restore
  the entire historical environment snapshot after subsequent billing/reward
  configuration changes: doing so could clear a newly activated cutoff or
  reopen generation. Preserve the economic settings and immutable release.

## Additional dispatch gap: legacy coordinator sampler

- Runtime inspection of `4fa8bb65` found `GRID_PROBE_ENABLED=1`, interval
  300 seconds, and a 256-token cap in the actual supervisor/child environment.
  The old `services/probe.py` submitted ordinary text jobs without reservation
  or an assignment-bound no-DEN terminal. Its evidence-only verdict framing
  did not prevent successful jobs from producing ordinary worker DEN.
- The retirement candidate removes the startup task, replaces old callable
  entry points with a fail-closed compatibility shim, and removes enabling
  instructions from the environment template. It leaves registered-validator
  assignments, manager setup canaries, and the separately budgeted audit rail
  unchanged. The updated probe document separates verdict authority from
  execution economics.
- A regression test first reproduced ordinary queue submission and now proves
  no submission even with the legacy flag set. Full local Grid suite: 1,271
  passed, 277 environment-dependent skips. Required hosted CI and deployment
  remain pending for this candidate; do not describe the production gap as
  closed merely because the source fix exists.
- At deployment, set the legacy enable flag to `0` while preserving all other
  settings, so rollback to the previous code cannot restart that sampler.
  Keep payouts paused and preserve historical ledger rows. This finding does
  not establish which historical payments came from these probes or attribute
  all unbilled traffic to abusive operators; reconciliation is separate.

## Legacy sampler retirement deployed

- PR #144 merged as `c34c7da511b640ef776e4a5c085d8a765c1cb12b`, with
  an identical tree to tested head `dfef9e49`. Required hosted run
  `34287466504` passed 1,539 Grid tests with 9 skips, 15 anti-gaming tests,
  PostgreSQL 16 backup/restore and schema parity, and the real
  Core/Console/node handoff. CodeQL and secret/infra scans passed.
- Immutable `grid-core-c34c7da5` became current at
  `2026-09-08T22:55:11Z`. Before cutover, the unchanged hash-locked dependency
  installation passed `pip check`. Fresh backup
  `grid-postgres-20260908T225308Z.dump` passed checksum and disposable restore
  proof against this candidate. Live schema parity remains Alembic `0039`;
  no production migration ran. Protected evidence and configuration snapshots
  are under `/var/lib/aipg-backup/demand-release-c34c7da5/`.
- The only parsed configuration change was `GRID_PROBE_ENABLED=0`.
  Supervisor and child processes use the exact new release and flag value;
  the new source does not start the sampler even if the old flag is restored.
  Registered-validator assignments and manager setup canaries were not changed.
  Nginx, service units, and timer states were preserved. API is active with
  zero automatic restarts, and both payout timer and sender remain inactive.
- All 12 pre-restart workers and 16 model identities were online again by
  `2026-09-08T22:56:16Z`, including the delayed Qwen 4B reconnect. Public
  validator status remained available with seven fresh heartbeats. These are
  recovery observations, not proof of new post-restart paid jobs or independent
  validator qualification.
- Post-cutover read-only reconciliation found six purchased-credit accounts,
  zero mismatches, zero negative balances, and zero net drift. No historical
  DEN, credit, or payout row was rewritten. Treasury/reward monitoring remains
  enabled; charging is still allowlisted, with the generation allowlist and
  prospective reward cutoff unset. This closes the old sampler's dispatch
  bypass, not all public billing or historical reward reconciliation.
- Code rollback is `grid-core-4fa8bb65` with schema `0039` retained and
  `GRID_PROBE_ENABLED=0` kept. Do not restore the pre-retirement environment:
  that would restart the unsafe sampler on the old code. Preserve any later
  economic configuration changes as well.

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
| Validator/worker setup probes | Bound dedicated no-DEN terminals; separate legacy coordinator sampler retired | Legacy sampler retirement deployed at `c34c7da5`; remaining live bound-path proof pending |

## Rollback boundary

Do not delete holds, ledger rows, payout rows, or nonce history. Preserve the
reward cutoff once activated. Stop generation on failing paths rather than
silently reopening unrestricted free traffic. Disabling charging only stops new
holds; existing holds still need settlement/release and monitoring. Keep payout
timers paused until reconciliation, even if demand charging is rolled back.
