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

- production Core runs immutable commit `0d850e73` with Alembic `0024`;
- `/v1/status/network` is live and reports five connected workers, nine online
  model entries, and every model below the three-worker redundancy target;
- shared-quorum text validation is live in `shared_quorum_preview` mode;
- three active first-party validators on commit `16e05327` completed two fresh
  3-of-5 groups with three distinct verified signatures and evidence
  commitments per group;
- those validators share one operator and hypervisor, so verified independent
  operator count remains zero;
- validator rewards, validator stake, worker penalties from validator evidence,
  and Core federation are off;
- charging remains a narrow allowlist canary rather than global; and
- no public validator binary release or production-capable media-manager
  release exists. A benchmark-only media-manager qualification prerelease is
  public, but it cannot enroll with the Grid or advertise capabilities.

Host verification on 2026-08-21 confirmed the immutable release and database
revision above and found no scheduled database backup or off-host backup
system. A supervised rehearsal at exact candidate `c73864ee` then created a
66,866,179-byte checksummed production snapshot, restored it into the guarded
scratch database, upgraded `0019` through `0024`, passed `alembic current`,
`heads`, and `check`, and removed the scratch database. The rehearsal first
exposed two real portability defects: the backup included unrelated `cron`
extension state, and a schema-scoped archive collided with the scratch
database's default `public` schema. Candidate commits `290c0375` and `c73864ee`
scope archives to the Grid-owned schema, explicitly reset only the generated
scratch schema, and make the PostgreSQL 16 restore proof a pull-request gate.
The hardened `aipg-postgres-backup.timer` is enabled and active; its first
unattended scheduled run remains pending, and no off-host backup system exists. A
separate PostgreSQL rehearsal upgraded `0019` to
`0024` with 100,000 synthetic legacy assignments and 110,000 synthetic legacy
attestations in place, preserved every row, completed locally in 0.66 seconds,
and passed `alembic check` with no schema drift. That synthetic rehearsal proves
the migration data shape and constraints; production restoration is evidenced
separately above, while production migration lock duration remains unmeasured.

## Completed Rollout Evidence

Core PR 21 and its security/documentation prerequisites are merged. The exact
Core release `0d850e73` was deployed immutably, migrated through `0024`, and
smoked with charging still restricted to the existing allowlist. Validator PRs
2-6 are merged through `16e05327`; no release tag, binary, or container image
was published.

Three separately keyed first-party nodes then completed two fresh shared text
groups. Each group had three assignments, three distinct validators, three
distinct Grid nonces, three completed probes, three evidence commitments, and
three verified authoritative signatures. One echo group reached `accepted /
healthy`; one tool-chain group reached `accepted / failed`. The latter is a
worker/capability result to investigate, not a validator transport failure.

Credit, reservation, den-event, payout, and worker-ledger row counts were
unchanged across the probes. A direct join from every fresh probe job id to
`grid_ledger.job_id` returned zero rows. Validator economics, staking, routing
effects, strikes, and slashing remain disabled.

## Immediate Validator Preview

### 1. Dedicated validator API scopes - Implemented/live

Core defines and enforces `validator.assignments`, `validator.probe`,
`validator.attest`, and `validator.read`. Console validator-purpose keys request
that bounded set. Evidence: `services/accounts.py`, `routers/validator.py`, and
validator route tests. Three production keys carrying exactly those scopes
registered and completed assignment-bound work on 2026-08-21.

### 2. Verified account-bound signing identity - Implemented/live

Registration and authoritative evidence recover an EIP-191 signing wallet and
bind it to the authenticated canonical account. Assignment id, group id, nonce,
and evidence commitment are signature inputs. Three separate production
accounts and signing wallets submitted six verified authoritative votes.

### 3. Fail-closed targeted probing - Implemented/live

Authoritative probing requires a Grid-issued assignment and passes an exact
`hard_target_worker`; missing assignment, registration, target, or capability
support produces no probe. There is no public-inference compatibility fallback.
Production hard-target smoke completed against both online text workers. Probe
job IDs did not enter the worker ledger or any demand/supply money table.

### 4. Validator registration and heartbeat - Implemented/live

Core stores validator id, signing wallet, account, software version,
capabilities, status, and heartbeat. Account and wallet uniqueness prevent
identity multiplication inside one canonical account. Production reports three
active heartbeat-fresh registrations.

### 5. `grid-validator v0.1.0-preview` - Ready, unpublished

Four-platform binaries, multi-architecture Docker builds, checksums, a
checksum-verifying installer, SPDX SBOM, and GitHub provenance workflows exist.
Fresh build-only GitHub runs and an independent clean-download verification of
the complete 85-package SPDX/checksum payload have passed. No tag, GitHub
Release, or registry image has been published. Publication remains gated on
platform signing, protected release approval, and clean-download verification;
the Core rollout and authenticated canary are complete.

### 6. Validator onboarding - Implemented

The public `/validate` page release-gates incomplete assets and independently
requires the live Core `shared_quorum_preview`, 3-of-5, scoped, non-economic
capability contract. A complete GitHub Release is still required to unlock
downloads. The page links scoped key creation, documents health
commands, and says there are no rewards, stake, slashing, or routing effects.
The Console has registration/scorecard surfaces. Downloads remain closed.

### 7. Five to ten independent preview operators - External

`PREVIEW_COHORT.md` defines safe enrollment and a 72-hour qualification run;
the website recruits the cohort. Three first-party nodes prove deployment and
quorum mechanics but count as one operator. No evidence currently proves five
independent operators. Until that exists, public language must remain
**distributed testing**, not decentralized validation.

Core candidate code adds the missing privacy-safe registry: opaque common-control
groups, rate-limited qualification samples, expiring reviews, one group per
quorum seat, and aggregate-only health. This is source-ready but does not change
the external status above until migration `0026` is dark-deployed and real
operators complete qualification.

## Real Quorum

### 8. Shared probe groups - Implemented/live

Core source persists one immutable challenge copy per group, distinct
validator assignments, independent attestations, and
pending/accepted/disputed/finalized states. New text groups are rate-bounded per
worker/model, and finalized operational rows are retention-bounded while signed
evidence remains durable. Two fresh production groups reached `accepted` with
unanimous three-validator evidence on 2026-08-21.

### 9. Distinct registered 3-of-5 quorum - Implemented, pilot-proven

Aggregation counts distinct registered validator ids, targets five, and accepts
at threshold three. Operator independence is a separate false-by-default field;
registration count does not claim independent control. The live pilot proved
three distinct identities and nonces, not three independent operators.

### 10. One authoritative vote per validator/group - Implemented/live

Migration `0022` and `schema.py` enforce unique
`(probe_group_id, validator_id)` membership for assignments and attestations.
Application duplicate handling is not the only guard.

### 11. Leases, replay defense, retries, and durable delivery - Ready

Core has atomic bounded probe leases and attempt limits (`0021` and later).
The node persists signed envelopes locally before submission, replays pending
evidence before new work, and removes it only after Core acceptance. Production
chaos behavior remains unproven.

### 12. Separate workflow states - Implemented/live

Core and Console distinguish probe execution, accepted evidence, worker
verdict, quorum outcome, and finalization. The Console renders preview-only
language. Production now contains accepted evidence while economic effects
remain disabled.

### 13. Aggregate validator health - Implemented/live

Core source reports registered, heartbeat-fresh, participating, and verified
independent counts; assignment stages; agreement/dispute rates; coverage;
quorum states; and bounded software cohorts. The public network status embeds
the privacy-safe subset and currently reports three active/fresh/participating
validators, zero verified independent operators, and two accepted groups.

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
green website rollout PR adds exact asset-set, digest, size, SBOM, manifest,
tag-commit, and platform-signing checks; bounds downloaded manifest and checksum
files before buffering; caches immutable release evidence for 24 hours; exposes
the benchmark-only media qualification tool as non-enrolling; and keeps final
downloads closed until verified releases exist. Its 29 release-gate tests and
eight desktop/mobile browser tests pass, including the real immutable
qualification release and deliberately incomplete legacy text release. That PR
remains unmerged because website `main` deploys production. Do not call this
item complete until the reviewed rollout is explicitly approved, deployed, and
checked on the production page.

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

### 19. Rich text validation - Ready, backend canaries pending

Implemented randomized lanes cover exact instruction, arithmetic, strict JSON,
calibrated 4K, 16K, and 32K context retrieval, multistep logic, one function
call, a two-stage tool chain, stop-sequence compliance, gross output-budget
compliance, and Python function synthesis checked against assignment-only hidden
inputs. The code lane never executes worker code: Core and each validator parse
one bounded function AST and independently interpret only integer arithmetic.
Context tiers require conservative worker-advertised headroom. Richer code and
logic tiers, 64K+ long-context tiers, longer tool chains, and streaming integrity
remain open; exact native-tokenizer equivalence is intentionally not claimed.
Those are future depth, not substitutes for live proof of the implemented
capability set. Production enablement still requires hard-targeted acceptance
and negative canaries against representative text backends.

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

### 30. Consolidate important work on main branches - Partial

The Core and validator rollout candidates described above are merged to their
protected default branches, and production runs those exact reviewed commits.
This does not establish that every side branch in the wider workspace is
obsolete: dated archive, upstream-merge, and superseded WIP branches remain
archaeology rather than production candidates. The local `aipg-oss-release`
toolkit is not a Git repository; its scanner template and generated staging
snapshots are therefore nondurable until that toolkit is either tracked or
regenerated from a reviewed source. Local deprecation/DOX commits also remain in
`aipg-horde-api`, `grid-rewards-sentry`, and the retired `grid-sdk`, whose
AIPowerGrid remotes now return repository-not-found, plus
`grid-discord-image-bot`, whose remote is archived read-only. The image bot
also retains pre-existing TypeScript failures and high-severity dependency
advisories. None is a current network component or merge candidate; do not
restore those remotes or revive the code merely to eliminate a local ahead
count.

### 31. Branch protection - Partial

Core, contracts, text worker, media worker, and validator require their current
CI/security status names. Validator `master` additionally enforces one approving
review, stale-review dismissal, admin compliance, linear history, resolved
conversations, and no force-push/deletion. The other protected repos also enforce
rules for administrators and do not permit force-push/deletion, but zero required
reviews remains a sole-maintainer tradeoff: a maintainer can merge or directly
commit once strict required checks pass without independent approval. Stale
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
bucket names, and developer paths. History-gate PRs now replace broad
path, placeholder, address, and test-value allowlists with exact reviewed
fingerprints or rule-scoped public constants across Core, text worker, media
worker, validator, frontend, website, contracts, documentation, and both current
SDKs. Every gate scans complete
reachable history with a checksum-verified scanner and uses a committed
synthetic private-key negative control to prove that example labels cannot hide
a future secret. The website, documentation, and SDK infrastructure scanners
also self-test runtime-constructed blocked patterns and distinguish a clean
no-match from scanner execution failure; this closed a false-green malformed
regular-expression path discovered during local reproduction. These gates
remain unmerged, so protected defaults are not yet uniformly hardened. Current
protected-branch worktrees scan clean. Text, media,
and validator release pipelines carry checksums, SBOM/provenance gates
appropriate to their maturity.
Text-worker `main` now requires the exact four-platform release-payload assembly
check on every change. Media-worker `main` records explicit Authenticode state,
runs Linux and Windows manager packaging on every change, and requires their
stable aggregate release gate. Validator `master` now requires one approving review,
stale-review dismissal, admin enforcement, strict CI, linear history, resolved
conversations, no force-push/deletion, and the exact four-platform payload check.
Validator PR 2 merged as `ffbc7db8`; it adds a commit/tag/version-bound release
manifest and blocks publication until verified macOS Developer ID/notarization
and Windows Authenticode state is recorded.
Core production
dependencies now resolve into a reviewed Python 3.12/Linux lock with exact
versions and package hashes; Docker, host bootstrap, and CI install that lock
with `--require-hashes` from binary wheels only, the Core image base is
digest-pinned, and release construction does not upgrade pip. CI regenerates
the lock and runs `pip-audit` before tests. Core source also exposes a build
commit in the status API and deploys immutable releases. Production reports the
exact deployed commit `0d850e73`. GitHub immutable releases were
enabled on 2026-08-21 for `grid-validator`, `grid-media-worker`, and
`grid-inference-worker`; their future published GitHub release tags and assets
cannot be replaced, and corrections require a new version. This setting does
not retroactively protect older releases, qualify draft artifacts, or replace
container-registry tag policy. Documentation PR 3 adds the organization-wide
requirement for full-history scans, burned-secret rotation before cleanup,
fingerprint-scoped baselines, signed release manifests/SBOMs, and exact
deployment SHA/digest records. It also updates the docs build within Next 15 and
Nextra 3 to versions with a zero-vulnerability `npm audit` result and a passing
33-route production build. SDK PR 2 in each current SDK adds the same fail-closed
history gate; the Python matrix passes 3.9-3.12, while the JavaScript candidate
preserves and tests the declared Node 18 floor by pinning a compatible Vitest
major. These changes remain unmerged, and implementation of the standard in
every repository remains periodic work.

### 33. Public network status - Implemented/live

Core source exposes privacy-safe worker/model redundancy, validator health,
payout totals, charging posture, incidents, advisories, build commit, and
architecture maturity. The public `/status` page is deployed and tested at
desktop/mobile widths. Production `/v1/status/network` returns the live
privacy-safe feed at build `0d850e73`.

### 34. Trusted-partner Core federation - Deferred design

`DECENTRALIZATION_ROADMAP.md` defines replay observers, signed event envelopes,
deterministic reducers, ingress partners, fenced write leases, controlled
failover, and later multi-authority ordering. No federation code is live. Begin
only after validator quorum, event replay, and economic-state invariants are
proven.

### 35. Verified database backup and restore - Partial/live

Core source creates locked PostgreSQL custom-format dumps, verifies archive
structure, binds one SHA-256 manifest to the exact dump, refuses overwrite,
uses root-only storage, and applies bounded local retention. The restore tool
accepts only local PostgreSQL, creates and drops only a generated
`aipg_restore_proof_*` database, restores as the application owner, migrates
with the exact immutable candidate, and requires `alembic current`, `heads`,
and `check` agreement. Pull-request CI rehearses `0019` backup through `0024`
restore on PostgreSQL 16 and proves a decoy non-Grid schema is excluded. The
supervised production proof passed at exact candidate `c73864ee`; the generated
scratch database was removed. The systemd units pass clean-environment
verification, and `aipg-postgres-backup.timer` is enabled and active. Its first
unattended scheduled run and an off-host copy/restore drill remain open. Local
retention is not off-host disaster recovery.

## Next Controlled Sequence

1. Observe the three-node first-party pilot for at least 72 hours, retain
   assignment/quorum/error metrics, and investigate the unanimous
   `deepseek-v4-flash-nvfp4` tool-chain failure before changing scoring or worker
   claims.
2. Prove the first unattended backup timer run, copy an encrypted snapshot
   off-host, and perform an off-host restore drill.
3. Close `grid-validator` issue 1 with verified macOS Developer ID/notarization
   and Windows Authenticode evidence, then publish the versioned validator
   prerelease and Docker image, verify every clean-download checksum/provenance
   artifact, and keep `latest` untouched.
4. Qualify 5-10 independent validator operators and measure them for 30 days.
5. Recruit at least two independent serving operators per flagship model while
   completing the real media-manager qualification evidence.
6. Expand charging only through reconciled allowlist stages.
7. Resolve owner-worker exclusion before any intentional no-payout internal
   fleet participates in a payout denominator.

Do not collapse these into one launch switch. Each stage has a separate
rollback boundary and produces evidence needed by the next stage.
