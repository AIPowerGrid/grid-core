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
- the live model status lists nine serving entries across text, image, video,
  and audio, each with one worker;
- validator capabilities in production are the older assignment-bound preview,
  not the source-ready shared-quorum contract;
- validator rewards, validator stake, worker penalties from validator evidence,
  and Core federation are off;
- charging remains a narrow allowlist canary rather than global; and
- no public validator binary release or qualifying media-manager release exists.

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

Manager packaging and fresh standalone Linux/Windows qualification builds pass,
but the release trust contract correctly blocks publication. Missing evidence
includes distinct real minimum/midrange/datacenter benchmark reports, an
on-chain RecipeVault root, offline profile signing, the exact signed active
profile, and platform-signing review. Do not replace those with synthetic
reports or an unsigned draft profile.

### 15. Independent text and media choices on `/run` - Implemented

The live page exposes the signed text-worker path independently and presents a
separate release-gated media-manager path. An unfinished media release no longer
hides text-worker onboarding.

### 16. Three independent workers per flagship model - External

The current public status reports one serving worker for every advertised
model entry. The target is three independently operated workers per flagship
model, requiring at least two additional operators for each currently
single-worker capability. This is the largest availability and decentralization
gap.

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
context retrieval, multistep logic, one function call, a two-stage tool chain,
and stop-sequence compliance. Hidden code execution, larger long-context tiers,
longer tool chains, streaming integrity, and token-budget honesty remain open.

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

### 22. Video validation lanes - Open

Duration, dimensions, frame count, motion, temporal consistency, prompt
relevance, and deterministic workflow comparison remain design work. Image
fidelity code must not be described as video validation.

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
reconciliation evidence before expansion.

### 25. One canonical account and balance - Implemented

Core owns canonical identity links, aliases, three credit pockets, and bounded
service delegation. Focused Core, Chat, Art, Music, and Console tests prove
delegated tokens and reject account mismatches. Google and wallet proof can
merge without rewriting historical ledgers. Every auth change still requires a
cross-product live canary because partial deployments have caused outages.

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
CI/security status names. Administrative bypass and zero required reviews
remain a sole-maintainer tradeoff, so protection does not prevent an admin from
pushing before checks finish. Stale status names must be reconciled whenever a
workflow matrix changes.

### 32. CI, scanning, provenance, and deploy identity - Partial

Key repos have tests, full-history/worktree secret scanning, CodeQL or language
security checks, dependency monitoring, and SHA-pinned release tooling where
artifacts exist. Text, media, and validator release pipelines carry checksums,
SBOM/provenance gates appropriate to their maturity. Core production
dependencies now resolve into a reviewed Python 3.12/Linux lock with exact
versions and package hashes; Docker, host bootstrap, and CI install that lock
with `--require-hashes` from binary wheels only, the Core image base is
digest-pinned, and release construction does not upgrade pip. CI regenerates
the lock and runs `pip-audit` before tests. Core source also exposes a build
commit in the candidate status API and deploys immutable releases, but
production still predates that candidate. A complete organization-wide policy
audit remains periodic work.

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

## Next Controlled Sequence

1. Back up production and prove restore/migrations on a scratch database.
2. Obtain explicit deployment authorization for the exact reviewed Core commit.
3. Deploy it immutably, migrate through `0024`, and verify build identity,
   charging flags, payout timer state, workers, model inventory, retired API
   behavior, shared-quorum capabilities, and unauthenticated validator denial.
4. Create a dedicated validator key and linked signing wallet, then run one
   authenticated end-to-end assignment without economic side effects.
5. Publish the versioned validator prerelease and Docker image, verify every
   clean-download checksum/provenance artifact, and keep `latest` untouched.
6. Qualify 5-10 independent validator operators and measure them for 30 days.
7. Recruit at least two independent serving operators per flagship model while
   completing the real media-manager qualification evidence.
8. Expand charging only through reconciled allowlist stages.
9. Resolve owner-worker exclusion before any intentional no-payout internal
   fleet participates in a payout denominator.

Do not collapse these into one launch switch. Each stage has a separate
rollback boundary and produces evidence needed by the next stage.
