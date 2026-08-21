# Network Readiness Ledger

**Evidence snapshot:** 2026-08-21 UTC

This is the durable status ledger for the Grid decentralization backlog. It
separates source implementation, production deployment, public distribution,
and independently operated network proof. A green unit test does not prove a
production rollout, and a registered node does not prove independent control.

## Status Vocabulary

- **Implemented** - source and focused verification exist.
- **Ready** - implementation is verified but a production/public rollout gate
  remains.
- **Partial** - useful implementation exists but the requirement is not met.
- **External** - code cannot satisfy the requirement without operators,
  hardware, or observed production evidence.
- **Deferred** - intentionally held behind an earlier safety gate.
- **Open** - no adequate implementation yet.

## Production Truth

At this snapshot:

- production Core runs immutable commit `20d57669` with Alembic `0019`;
- reviewed Core main is schema-complete through `0024`, but is not deployed;
- `/v1/status/network` returns `404` in production, while the public `/status`
  page is deployed in an honest feed-unavailable state;
- live Core health reports five connected workers and seven available model IDs
  across text, image, video, and audio; production does not yet expose the
  candidate's privacy-safe per-model redundancy feed;
- validator capabilities in production are the older assignment-bound preview,
  not the source-ready shared-quorum contract;
- validator rewards, validator stake, worker penalties from validator evidence,
  and Core federation are off;
- charging remains a narrow allowlist canary rather than global; and
- no public validator binary release or production-capable media-manager
  release exists. A benchmark-only media-manager qualification prerelease is
  public, but it cannot enroll with the Grid or advertise capabilities.

Read-only host verification on 2026-08-21 confirmed the immutable release and
database revision above and found no scheduled database backup or off-host
backup system. Core source now includes a locked, checksummed custom-format
backup, hardened daily systemd timer, and guarded scratch restore/migration
proof. The complete source path has passed twice against PostgreSQL 16, but the
timer remains disabled and no production snapshot has been exercised. A
separate PostgreSQL rehearsal upgraded `0019` to
`0024` with 100,000 synthetic legacy assignments and 110,000 synthetic legacy
attestations in place, preserved every row, completed locally in 0.66 seconds,
and passed `alembic check` with no schema drift. This proves the migration data
shape and constraints, not production lock duration or backup restoration.

## Immediate Validator Preview

### 1. Dedicated validator API scopes - Ready

Core defines and enforces `validator.assignments`, `validator.probe`,
`validator.attest`, and `validator.read`. Console validator-purpose keys request
that bounded set. Evidence: `services/accounts.py`, `routers/validator.py`, and
validator route tests. Remaining gate: deploy the matching Core and smoke a
fresh purpose-bound key.

### 2. Verified account-bound signing identity - Ready

Registration and authoritative evidence recover an EIP-191 signing wallet and
bind it to the authenticated canonical account. Assignment id, group id, nonce,
and evidence commitment are signature inputs. Remaining gate: production
registration plus negative tests against an unrelated linked wallet.

### 3. Fail-closed targeted probing - Ready

Authoritative probing requires a Grid-issued assignment and passes an exact
`hard_target_worker`; missing assignment, registration, target, or capability
support produces no probe. There is no public-inference compatibility fallback.
Remaining gate: production hard-target smoke against a controlled worker.

### 4. Validator registration and heartbeat - Ready

Core stores validator id, signing wallet, account, software version,
capabilities, status, and heartbeat. Account and wallet uniqueness prevent
identity multiplication inside one canonical account. Remaining gate:
production migration `0020+` and live registration/heartbeat proof.

### 5. `grid-validator v0.1.0-preview` - Ready, unpublished

Four-platform binaries, multi-architecture Docker builds, checksums, a
checksum-verifying installer, SPDX SBOM, and GitHub provenance workflows exist.
Fresh build-only GitHub runs and an independent clean-download verification of
the complete 85-package SPDX/checksum payload have passed. No tag, GitHub
Release, or registry image has been published. Publication remains gated on
the Core rollout and authenticated canary.

### 6. Validator onboarding - Implemented

The public `/validate` page release-gates incomplete assets and independently
requires the live Core `shared_quorum_preview`, 3-of-5, scoped, non-economic
capability contract. A complete GitHub Release cannot unlock downloads against
the older production Core. The page links scoped key creation, documents health
commands, and says there are no rewards, stake, slashing, or routing effects.
The Console has registration/scorecard surfaces. Downloads remain closed.

### 7. Five to ten independent preview operators - External

`PREVIEW_COHORT.md` defines safe enrollment and a 72-hour qualification run;
the website recruits the cohort. No evidence currently proves five independent
operators. Until that exists, public language must remain **distributed
testing**, not decentralized validation.

## Real Quorum

### 8. Shared probe groups - Ready

Core source persists one immutable challenge group, distinct assignments,
independent attestations, and pending/accepted/disputed/finalized states.
Production still runs the older Core.

### 9. Distinct registered 3-of-5 quorum - Ready

Aggregation counts distinct registered validator ids, targets five, and accepts
at threshold three. Operator independence is a separate false-by-default field;
registration count does not claim independent control.

### 10. One authoritative vote per validator/group - Ready

Migration `0022` and `schema.py` enforce unique
`(probe_group_id, validator_id)` membership for assignments and attestations.
Application duplicate handling is not the only guard.

### 11. Leases, replay defense, retries, and durable delivery - Ready

Core has atomic bounded probe leases and attempt limits (`0021` and later).
The node persists signed envelopes locally before submission, replays pending
evidence before new work, and removes it only after Core acceptance. Production
chaos behavior remains unproven.

### 12. Separate workflow states - Implemented

Core and Console distinguish probe execution, accepted evidence, worker
verdict, quorum outcome, and finalization. The Console renders preview-only
language. Production data will remain empty until rollout and operators exist.

### 13. Aggregate validator health - Ready

Core source reports registered, heartbeat-fresh, participating, and verified
independent counts; assignment stages; agreement/dispute rates; coverage;
quorum states; and bounded software cohorts. The public network status embeds
the privacy-safe subset. Production endpoint rollout is pending.

## Worker Growth

### 14. Public media-worker manager release - External/Partial

The immutable `manager-qualification-v0.1.0-preview.1` prerelease now publishes
standalone Linux and Windows benchmark tools, `SHA256SUMS`, an SPDX SBOM, a
machine-readable restriction manifest, and GitHub build-provenance attestations.
An independent clean download verified every checksum, manifest gate, and
attestation. The bundled profile is deliberately unsigned: the tool can install
and benchmark locally, but Core enrollment and capability advertisement both
fail closed.

This does **not** satisfy the production-manager release requirement. Missing
evidence still includes distinct real minimum/midrange/datacenter benchmark
reports, an on-chain RecipeVault root, offline profile signing, the exact signed
active profile, Windows Authenticode, and supervised Linux/Windows staging.
Media-worker `main` now records explicit signing state in the final manifest,
builds both platforms on every change, and requires the stable `Manager release
gate` in branch protection. Those source and CI controls prevent an unsigned or
unbuilt candidate from appearing ready; they do not supply the missing hardware
or signing evidence. Do not replace those with synthetic reports or promote the
qualification tag to a production download.

### 15. Independent text and media choices on `/run` - Partial, hardened rollout pending

The production page presents text and media independently, so an unfinished
media release does not hide text onboarding. Its current release lookup is not
yet the authoritative gate: it accepts published asset names without validating
the complete manifest/checksum contract or platform-signing identities. The
green website rollout PR adds exact asset-set, digest, size, SBOM, manifest, and
platform-signing checks; exposes the benchmark-only media qualification tool as
non-enrolling; and keeps final downloads closed until verified releases exist.
That PR remains unmerged because website `main` deploys production. Do not call
this item complete until the reviewed rollout is explicitly approved, deployed,
and checked against both valid and deliberately incomplete release fixtures.

### 16. Three independent workers per flagship model - External

Live Core `/health` reports five connected workers and seven currently available
model IDs. The public `/v1/status/models` inventory was checked on 2026-08-21 and
reported **one serving worker for every one of its nine online entries** across
text, image, video, and audio. In particular, `gpt-oss-120b` and
`deepseek-v4-flash-nvfp4` each had one serving worker while the 30-day public
statistics reported 4,448 and 4,252 jobs respectively. The target remains three
independently controlled operators per flagship model; several processes,
wallets, or GPUs under one operator count once. This is the largest current
availability and decentralization gap. Text cohort evidence is tracked in
`grid-text-worker` issue 10 and media supply in `grid-media-worker` issue 9.

### 17. Hardware-aware operator recommendation - Implemented

`/run` uses browser-local OS, accelerator type and model, VRAM, RAM, disk, and
expected or measured text throughput to recommend a worker path. Signed worker
profiles remain the final local capability authority.

### 18. Demand and opportunity without promises - Implemented

`/run` combines public worker counts, recent jobs, jobs per worker, and observed
network telemetry. It separates single-worker resilience risk from historical
workload share and labels both as planning signals rather than a hardware
benchmark, payout forecast, or fixed earnings promise.

## Better Validation

### 19. Rich text validation - Partial

Implemented randomized lanes cover exact instruction, arithmetic, strict JSON,
calibrated 4K and 16K context retrieval, multistep logic, one function call, a
two-stage tool chain, stop-sequence compliance, and gross output-budget
compliance using an independent model-agnostic token counter with
cross-tokenizer tolerance. Context tiers require conservative worker-advertised
headroom. Hidden code execution, 32K+ long-context tiers, longer tool chains,
and streaming integrity remain open; exact native-tokenizer equivalence is
intentionally not claimed.

### 20. Private deterministic image validation - Ready dark

Core generates private randomized prompts and seeds, dispatches one candidate
and two rotating references, computes stored-object SHA-256 witnesses, and the
node performs bounded fetch, structural, pHash, and reference-consensus checks.
The path is fail-closed and production-disabled because the recipe and reference
pool gates are not met.

### 21. Remove public fixed media challenges - Implemented

Runtime media challenge seeds use cryptographic randomness. Public numeric
seeds remain only in tests and illustrative documentation, not the live
challenge generator. Live prompt banks, golden pHashes, answer keys, and
private thresholds are forbidden from public binaries and repos.

### 22. Video validation lanes - Partial/Dark

The validator source has fail-closed `video.contract.v1` and
`video.fidelity.v1` scorers: authenticated MP4/WebM witnesses are decoded in a
killable bounded child process; dimensions, frame count, fps, duration,
timestamps, blank/static frames, and latency are checked; and fidelity compares
per-frame pHashes plus a lightweight motion profile only after two references
agree. Malformed assignments, unsafe fetches, local decoder timeouts, and
reference decode failures/disagreement are inconclusive; authenticated
malformed candidate bytes may fail. Core now has a separately gated,
default-off `video.contract.v1` assignment and hard-targeted MP4 witness path.
It requires a governed text-to-video recipe with explicit dimensions and timing;
it uses no references and makes no model-fidelity claim. Real LTX/workflow
calibration, governed recipe publication, prompt/key-event relevance, and
media-enabled release-binary qualification remain open. Reference-based
`video.fidelity.v1` remains disabled. No video evidence affects routing,
rewards, strikes, bonds, or slashing.

### 23. Rotating bonded references - Ready dark/External

Core has a fail-closed finalized-bond snapshot cache and distinct
account/wallet rotation selector. The eligible pool is empty because the
reviewed cooldown-backed WorkerRegistry deployment and independent reference
operators do not exist. No permanent trusted worker is accepted as the oracle.

## Demand And Economics

### 24. Staged charging expansion - Partial

The safe rollout machinery exists and production remains a narrow account/model
allowlist with global charging off and free spendable value off. All-media,
text, audio, internal-account, selected-external, and global stages require
reconciliation evidence before expansion. The current account has three
settled canary jobs (delegated Chat text plus Krea and Z-Image through Art), no
held or stale reservations, no negative balance, and no purchased-ledger drift.
That proves only a partial canary: Music, video, raw Responses/Messages,
disconnect, forced failure/refund, pre-queue `402`, retry/duplicate terminal,
and the 24-hour unchanged-cohort window remain unproven. The remaining canary
balance is below the currently selected model's minimum charge; further real
work requires deliberate funding approval.

### 25. One canonical account and balance - Source-complete, live parity pending

Core owns canonical identity links, aliases, three credit pockets, and bounded
service delegation. Focused Core, Chat, Art, Music, and Console tests prove
delegated tokens and reject account mismatches. Google and wallet proof can
merge without rewriting historical ledgers. Every auth change still requires a
cross-product live canary because partial deployments have caused outages. The
required live Google run and separate wallet run across Console, Chat, Art,
Music, and direct API use have not yet been captured as retained, `no-store`
receipt sets, so code-level completion must not be mistaken for
production-parity evidence.

### 26. Explicit owner-worker reward policy - Open, payout-backlog

Current custodial aggregation includes every attributed account in the den
denominator. An account without a payout wallet receives an `accrued` share,
which remains claimable and reduces everyone else's share. That is wrong for an
owner-operated fleet that intentionally declines emissions. The eventual
policy needs a durable, audited `pay` versus `exclude` decision made before
period aggregation; `exclude` must remove internal den from the denominator and
must not create a later claim. Do not implement this as an undocumented wallet
absence or mutable environment-only allowlist.

## Blockchain Phase

### 27. Do not deploy validator staking yet - Enforced decision

Preview stake is disabled and no validator economic contract is authorized for
deployment before independent quorum evidence.

### 28. Registry, bond, roots, disputes, rewards, slashing - Deferred

The buildable boundary is specified in `DECENTRALIZATION_ROADMAP.md`; contracts,
deployment scripts, independent audit, funded reward distributor, and dispute
operation do not yet exist. Slashing begins only with objective fraud.

### 29. Keep hot/private data off-chain - Implemented architecture

Base is reserved for identities, bonds, compact epoch/reward commitments,
claims, and dispute outcomes. Prompts, outputs, private challenge material,
credentials, hot queue state, and inference routing stay off-chain.

## Operations

### 30. Consolidate important work on main branches - Implemented with archives

Current production repos have clean worktrees synchronized to their upstream
main/master branches. Remaining side branches are dated archive, upstream-merge,
or superseded WIP histories; none is a newer production release candidate.
Preserve them as archaeology until deliberately retired rather than merging
them wholesale.

### 31. Branch protection - Partial

Core, contracts, text worker, media worker, and validator require their current
CI/security status names. Validator `master` additionally enforces one approving
review, stale-review dismissal, admin compliance, linear history, resolved
conversations, and no force-push/deletion. Administrative bypass and zero
required reviews remain a sole-maintainer tradeoff on the other protected repos,
so their rules do not prevent an admin from pushing before checks finish. Stale
status names must be reconciled whenever a workflow matrix changes. Media-worker
`main` now requires a stable final manager-release gate in addition to its test,
dependency, and secret checks; the gate itself depends on both platform builds.

### 32. CI, scanning, provenance, and deploy identity - Partial

Key repos have tests, tracked-worktree secret scanning, CodeQL or language
security checks, dependency monitoring, and SHA-pinned release tooling where
artifacts exist. A redacted full-history scan of seven critical repositories on
2026-08-21 classified six distinct historical 22-character API credentials
across Core, media worker, and contracts. A read-only request to the production
account endpoint rejected every one with HTTP 401; no active production
credential was found and no credential value was copied into the audit record.
The other findings were deterministic local-service fixture values, public
cryptographic constants/test vectors in a retired paper-wallet bundle,
documented placeholders, and operational metadata such as retired internal IPs,
bucket names, and developer paths. Full-history scanning is still not a clean
enforced gate: those reachable findings require either coordinated history
repair or exact-fingerprint baselines with rationale and review dates. Current
protected-branch worktrees scan clean. Text, media, and validator release
pipelines carry checksums, SBOM/provenance gates appropriate to their maturity.
Text-worker `main` now requires the exact four-platform release-payload assembly
check on every change. Media-worker `main` records explicit Authenticode state,
runs Linux and Windows manager packaging on every change, and requires their
stable aggregate release gate. Validator `master` now requires one approving review,
stale-review dismissal, admin enforcement, strict CI, linear history, resolved
conversations, no force-push/deletion, and the exact four-platform payload check.
Validator PR 2 adds a commit/tag/version-bound release manifest and blocks
publication until verified macOS Developer ID/notarization and Windows
Authenticode state is recorded; it remains unmerged pending independent review.
Core production
dependencies now resolve into a reviewed Python 3.12/Linux lock with exact
versions and package hashes; Docker, host bootstrap, and CI install that lock
with `--require-hashes` from binary wheels only, the Core image base is
digest-pinned, and release construction does not upgrade pip. CI regenerates
the lock and runs `pip-audit` before tests. Core source also exposes a build
commit in the candidate status API and deploys immutable releases, but
production still predates that candidate. GitHub immutable releases were
enabled on 2026-08-21 for `grid-validator`, `grid-media-worker`, and
`grid-inference-worker`; their future published GitHub release tags and assets
cannot be replaced, and corrections require a new version. This setting does
not retroactively protect older releases, qualify draft artifacts, or replace
container-registry tag policy. Documentation PR 3 adds the organization-wide
requirement for full-history scans, burned-secret rotation before cleanup,
fingerprint-scoped baselines, signed release manifests/SBOMs, and exact
deployment SHA/digest records. It remains unmerged, and implementation of that
standard in every repository remains periodic work.

### 33. Public network status - Ready, endpoint rollout pending

Core source exposes privacy-safe worker/model redundancy, validator health,
payout totals, charging posture, incidents, advisories, build commit, and
architecture maturity. The public `/status` page is deployed and tested at
desktop/mobile widths. It currently shows feed unavailable because production
Core returns `404`; it will populate after the approved Core rollout.

### 34. Trusted-partner Core federation - Deferred design

`DECENTRALIZATION_ROADMAP.md` defines replay observers, signed event envelopes,
deterministic reducers, ingress partners, fenced write leases, controlled
failover, and later multi-authority ordering. No federation code is live. Begin
only after validator quorum, event replay, and economic-state invariants are
proven.

### 35. Verified database backup and restore - Ready, production proof pending

Core source creates locked PostgreSQL custom-format dumps, verifies archive
structure, binds one SHA-256 manifest to the exact dump, refuses overwrite,
uses root-only storage, and applies bounded local retention. The restore tool
accepts only local PostgreSQL, creates and drops only a generated
`aipg_restore_proof_*` database, restores as the application owner, migrates
with the exact immutable candidate, and requires `alembic current`, `heads`,
and `check` agreement. CI rehearses `0019` backup through `0024` restore on
PostgreSQL 16. The systemd units pass clean-environment verification. The
remaining gate is a supervised production backup plus scratch restore before
first enablement. Local retention is not off-host disaster recovery.

## Next Controlled Sequence

1. Run the candidate backup tool against production and prove that exact
   snapshot in its guarded scratch database. Source-level PostgreSQL 16 backup,
   restore, `0019` to `0024` migration, schema-parity, and cleanup rehearsals
   are complete, but a production snapshot has not been exercised.
2. Obtain explicit deployment authorization for the exact reviewed Core commit.
3. Deploy it immutably, migrate through `0024`, and verify build identity,
   charging flags, payout timer state, workers, model inventory, retired API
   behavior, shared-quorum capabilities, and unauthenticated validator denial.
4. Create a dedicated validator key and linked signing wallet, then run one
   authenticated end-to-end assignment without economic side effects.
5. Close `grid-validator` issue 1 with verified macOS Developer ID/notarization
   and Windows Authenticode evidence, then publish the versioned validator
   prerelease and Docker image, verify every clean-download checksum/provenance
   artifact, and keep `latest` untouched.
6. Qualify 5-10 independent validator operators and measure them for 30 days.
7. Recruit at least two independent serving operators per flagship model while
   completing the real media-manager qualification evidence.
8. Expand charging only through reconciled allowlist stages.
9. Resolve owner-worker exclusion before any intentional no-payout internal
   fleet participates in a payout denominator.

Do not collapse these into one launch switch. Each stage has a separate
rollback boundary and produces evidence needed by the next stage.
