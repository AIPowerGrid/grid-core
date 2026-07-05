# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""system-core v2 schema — ledger-first, chain-anchored, dialect-portable.

Design rules (see docs/V2.md):

* The chain is the source of truth for anything consensus-critical (model
  registry, settlement roots, payments). Off-chain state is either a
  **ledger** (append-only, Merkle-izable, auditable against on-chain roots)
  or a **cache** (rebuildable, prunable).
* Every type here is dialect-portable (works on PostgreSQL *and* SQLite) so a
  community gateway can run the same code as prod with zero infrastructure —
  one binary + an embedded DB. Postgres gets JSONB via a type variant.
* Tables are namespaced ``grid_`` so they coexist with (and outlive) the
  legacy Haidra tables during the v2 transition.

Ledger vs cache:

==================  =======================================================
grid_ledger         TRUTH. Append-only job-completion events: den earned +
                    prompt/result hashes. Merkle root per epoch → on-chain.
grid_epochs         TRUTH (mirror). Settlement periods + their on-chain root.
grid_accounts,      Operational state. Identity is ultimately the wallet;
grid_api_keys       these rows make it usable (keys, oauth, email).
grid_workers        Cache-ish registry; presence/stats are rebuildable.
grid_jobs           CACHE. Hot queue state; prunable after completion since
                    the ledger holds the durable record.
==================  =======================================================
"""

from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON that is JSONB on Postgres (indexable) and plain JSON elsewhere
# (SQLite gateway-in-a-box mode).
PortableJSON = sa.JSON().with_variant(JSONB(), "postgresql")


# ── Identity ─────────────────────────────────────────────────────────────
# One account, up to three credential types. The wallet is the canonical
# cross-system identity (chat, API, workers, gallery, payouts); email/oauth
# exist for users who haven't connected a wallet yet. API keys are derived
# credentials — many per account, individually revocable.

accounts = sa.Table(
    "grid_accounts",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True, default=uuid4),
    sa.Column("wallet", sa.String(42), unique=True, nullable=True, index=True),
    # Payout address for worker earnings. Distinct from `wallet` (the login
    # identity): NOT unique, NOT a credential — an OAuth/username operator can
    # point earnings at any Base address, mining-style, no ownership proof.
    # Settlement prefers this; falls back to `wallet`.
    sa.Column("payout_wallet", sa.String(42), nullable=True),
    sa.Column("email", sa.String(254), unique=True, nullable=True),
    sa.Column("oauth_sub", sa.String(255), unique=True, nullable=True),
    sa.Column("username", sa.String(100), nullable=True),
    # admin / trusted / paid-tier flags etc. Schema-free on purpose: flags
    # change faster than migrations should.
    sa.Column("flags", PortableJSON, nullable=False, default=dict),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("last_active", sa.DateTime(timezone=True), nullable=True),
)

api_keys = sa.Table(
    "grid_api_keys",
    metadata,
    # SHA-256(GRID_SALT + key), hex — the plaintext key is never stored.
    sa.Column("hash", sa.String(64), primary_key=True),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("label", sa.String(100), nullable=True),
    # True ONLY for wallet-proven session keys (SIWE wallet-login / dashboard
    # login) and an account's initial login key — never for user-issued keys via
    # /v1/account/keys. Account-admin actions (change payout wallet, issue/revoke
    # keys) require a session key, so a leaked inference key can't redirect
    # earnings. Not caller-settable (issue_key forces False).
    sa.Column("is_session", sa.Boolean, nullable=False, server_default=sa.text("false"), default=False),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked", sa.Boolean, nullable=False, default=False),
)


# ── Workers ──────────────────────────────────────────────────────────────
# Registered over WS with a wallet signature. `wallet` is denormalized from
# the registration signature so payouts survive account edits.

workers = sa.Table(
    "grid_workers",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True, default=uuid4),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    sa.Column("name", sa.String(120), unique=True, nullable=False),
    sa.Column("type", sa.String(16), nullable=False),  # text | image | video
    sa.Column("wallet", sa.String(42), nullable=True, index=True),
    sa.Column("models", PortableJSON, nullable=False, default=list),
    sa.Column("capabilities", PortableJSON, nullable=False, default=dict),
    sa.Column("bridge_agent", sa.String(120), nullable=True),
    sa.Column("maintenance", sa.Boolean, nullable=False, default=False),
    sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True, index=True),
    # Running counters; authoritative totals always derivable from the ledger.
    sa.Column("jobs_completed", sa.BigInteger, nullable=False, default=0),
    sa.Column("den_earned", sa.Float, nullable=False, default=0.0),
)


# ── Jobs (cache) ─────────────────────────────────────────────────────────
# Hot dispatch state for all job types. Rows are prunable once finished —
# the ledger carries the durable record. status: queued → dispatched →
# done | faulted | cancelled.

jobs = sa.Table(
    "grid_jobs",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True, default=uuid4),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("type", sa.String(16), nullable=False),  # text | image | video
    sa.Column("status", sa.String(16), nullable=False, default="queued", index=True),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("payload", PortableJSON, nullable=False, default=dict),
    sa.Column("result", PortableJSON, nullable=True),
    sa.Column("worker_id", sa.Uuid, sa.ForeignKey("grid_workers.id"), nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("den", sa.Float, nullable=False, default=0.0),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
    sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
)


# ── Ledger (truth) ───────────────────────────────────────────────────────
# One append-only event per completed job: who did the work, what it earned,
# and the content hashes that make it attestable. Per epoch, events hash
# deterministically into a Merkle tree whose root settles on-chain
# (RewardPool/DenReporter on Base). Never UPDATE or DELETE rows here.

ledger = sa.Table(
    "grid_ledger",
    metadata,
    # BigInteger on Postgres; Integer on SQLite so the autoincrement PK actually
    # works there (BIGINT PKs don't autoincrement on SQLite) — same dialect-portable
    # fix as grid_credit_ledger, keeps the gateway-in-a-box / test path honest.
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
              primary_key=True, autoincrement=True),
    sa.Column("epoch_id", sa.String(32), nullable=True, index=True),  # set at settlement
    # Unique: one settled completion per job. Makes record_completion idempotent
    # so a stale-job reclaim + the original worker both finishing can't double-pay.
    sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
    sa.Column("worker_id", sa.Uuid, nullable=False, index=True),
    sa.Column("wallet", sa.String(42), nullable=True, index=True),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("job_type", sa.String(16), nullable=False),
    sa.Column("den", sa.Float, nullable=False, default=0.0),
    sa.Column("output_units", sa.Integer, nullable=False, default=0),  # tokens / images / frames
    # Performance telemetry (not economic — never feeds den/settlement). Used to
    # compute per-model t/s, TTFT, and avg latency in /v1/status|stats/models.
    # Nullable: historical rows + media jobs (ttft) carry NULL and are excluded
    # from the aggregates.
    sa.Column("duration", sa.Float, nullable=True),  # wall-clock generation seconds
    sa.Column("ttft", sa.Float, nullable=True),      # time-to-first-token seconds (text only)
    # sha256 of canonicalized prompt payload and result bytes — the
    # attestation hook for future verifiable inference.
    sa.Column("prompt_hash", sa.String(64), nullable=True),
    sa.Column("result_hash", sa.String(64), nullable=True),
    # Reserved: worker-signed receipt (wallet signature over the hashes).
    sa.Column("worker_sig", sa.String(132), nullable=True),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
)


# ── Credits (prepaid demand-side balance) ────────────────────────────────
# Prepaid balance in integer micro-USD (USD × 1e6). One row per account.
# A USDC deposit credits 1:1 (micro-USD); a completion debits per `pricing`. The
# balance is a cache of the ledger sum — `grid_credit_ledger` is the truth and
# makes every credit/debit idempotent (unique `ref`). See services/credits.py.
# Ships dark (GRID_CHARGING_ENABLED=0): the request path only logs would-charge.

credits = sa.Table(
    "grid_credits",
    metadata,
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("balance_micro", sa.BigInteger, nullable=False, default=0),
    sa.Column("updated", sa.DateTime(timezone=True), nullable=False, default=utcnow),
)

credit_ledger = sa.Table(
    "grid_credit_ledger",
    metadata,
    # BigInteger on Postgres; Integer on SQLite so the autoincrement PK actually
    # works there (BIGINT PKs don't autoincrement on SQLite) — keeps the
    # "dialect-portable" promise true for the gateway-in-a-box / test path.
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # Signed micro-USD: positive = top-up, negative = charge.
    sa.Column("delta_micro", sa.BigInteger, nullable=False),
    sa.Column("reason", sa.String(64), nullable=False),
    # Idempotency key: the charged job_id (debit) or deposit/Stripe event id
    # (credit). UNIQUE so a retried request / re-seen deposit can't double-apply.
    sa.Column("ref", sa.String(128), nullable=True, unique=True),
    sa.Column("model", sa.String(255), nullable=True),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
)


# Durable per-job reservation state. A reserve writes one 'held' row before
# dispatch; the worker-WS handler (the authority that reaches a terminal state
# for EVERY job regardless of whether the client stayed connected) flips it
# held→settled exactly once and reconciles/refunds against the actual
# grid-counted usage. Keyed by job_id so settlement never depends on the HTTP
# response collector. account_id/reserved are 0/NULL-tolerant for dry-run rows.
reservations = sa.Table(
    "grid_reservations",
    metadata,
    sa.Column("job_id", sa.String(64), primary_key=True),
    sa.Column("account_id", sa.Uuid, nullable=True),  # nullable: legacy/dry-run jobs
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("reserved_micro", sa.BigInteger, nullable=False, default=0),
    sa.Column("prompt_toks", sa.Integer, nullable=False, default=0),
    # 'held' until a terminal state settles it; the held→settled UPDATE is the
    # exactly-once guard (only the winning UPDATE moves money).
    sa.Column("status", sa.String(16), nullable=False, default="held", index=True),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
    sa.Column("settled", sa.DateTime(timezone=True), nullable=True),
)


# Custodial worker payouts (v1, pre-on-chain): one row per (period, account).
# Den is attributed to the ACCOUNT (the worker authenticates with its account key),
# so earnings never strand for lack of a wallet — an account with no payout_wallet
# yet is recorded 'accrued' (owed) and paid the moment it sets one. UNIQUE(period_id,
# account_id) is the idempotency guard. Same den source (grid_ledger) as the future
# on-chain Merkle-claim rail.
payouts = sa.Table(
    "grid_payouts",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
              primary_key=True, autoincrement=True),
    sa.Column("period_id", sa.String(48), nullable=False, index=True),
    sa.Column("account_id", sa.Uuid, nullable=True, index=True),     # owed-to identity
    sa.Column("address", sa.String(42), nullable=True),              # wallet paid (null while accrued)
    sa.Column("den", sa.Float, nullable=False, default=0.0),
    sa.Column("aipg_amount", sa.Numeric(38, 18), nullable=False),    # AIPG (whole tokens)
    # accrued (owed, no wallet yet) → pending → sent (tx broadcast) → confirmed | failed
    sa.Column("status", sa.String(16), nullable=False, default="accrued", index=True),
    sa.Column("tx_hash", sa.String(66), nullable=True),
    # The treasury nonce this payout is BOUND to. A payment is settled iff this
    # nonce has mined, and each nonce carries exactly one payout — so it can pay
    # at most once. Retries replace at this nonce, never a new one (double-pay-proof).
    sa.Column("nonce", sa.BigInteger, nullable=True),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("paid", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("period_id", "account_id", name="uq_grid_payouts_period_acct"),
)

# Hard guarantee that two payouts can never bind the SAME treasury nonce (so a
# concurrent runner can't allocate a duplicate) — partial unique on non-null nonce.
sa.Index("uq_grid_payouts_nonce", payouts.c.nonce, unique=True,
         postgresql_where=payouts.c.nonce.isnot(None),
         sqlite_where=payouts.c.nonce.isnot(None))


# ── Epochs / settlement (truth, mirrors chain) ──────────────────────────

epochs = sa.Table(
    "grid_epochs",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),  # e.g. "2026-06-11"
    sa.Column("opened", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("closed", sa.DateTime(timezone=True), nullable=True),
    sa.Column("total_den", sa.Float, nullable=False, default=0.0),
    sa.Column("merkle_root", sa.String(66), nullable=True),
    sa.Column("aipg_paid", sa.Numeric(38, 18), nullable=True),
    sa.Column("tx_hash", sa.String(66), nullable=True),
    sa.Column("finalized", sa.Boolean, nullable=False, default=False),
)


# ── Validator assignments / attestations (evidence, non-economic in V0) ──
# Assignments are the gate between preview canaries and authoritative evidence:
# only Grid-issued assignment ids + nonces can produce assignment-bound
# attestations. Quorum/status fields are intentionally off-chain and
# non-economic until dispute/reward contracts exist.
validator_assignments = sa.Table(
    "grid_validator_assignments",
    metadata,
    sa.Column("id", sa.String(96), primary_key=True),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("validator_wallet", sa.String(42), nullable=True, index=True),
    sa.Column("grid_nonce", sa.String(128), nullable=False, unique=True),
    sa.Column("target_worker_id", sa.String(64), nullable=False, index=True),
    sa.Column("target_worker_name", sa.String(120), nullable=False, index=True),
    sa.Column("model", sa.String(255), nullable=False, index=True),
    sa.Column("modality", sa.String(16), nullable=False, default="text"),
    sa.Column("capability", sa.String(128), nullable=False),
    sa.Column("canary_kind", sa.String(64), nullable=False),
    sa.Column("scoring_policy_id", sa.String(128), nullable=False),
    sa.Column("challenge", PortableJSON, nullable=False, default=dict),
    sa.Column("status", sa.String(24), nullable=False, default="pending", index=True),
    sa.Column("quorum_status", sa.String(24), nullable=False, default="pending", index=True),
    sa.Column("quorum_outcome", sa.String(24), nullable=True),
    sa.Column("probe_job_id", sa.String(96), nullable=True, index=True),
    sa.Column("probe_status", sa.String(24), nullable=False, default="not_started"),
    # Filled only after the Grid actually receives a hard-targeted probe reply.
    # Authoritative attestations must match this hash; a nonce alone is not
    # enough to claim assignment-bound evidence.
    sa.Column("probe_prompt_hash", sa.String(64), nullable=True),
    sa.Column("probe_response_hash", sa.String(64), nullable=True),
    sa.Column("probe_evidence_hash", sa.String(64), nullable=True, index=True),
    sa.Column("probe_verdict", sa.String(16), nullable=True),
    sa.Column("probe_latency_ms", sa.Integer, nullable=True),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
    sa.Column("expires", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("probed", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finalized", sa.DateTime(timezone=True), nullable=True),
)

# Signed validator reports about probe outcomes. V0 stores these as audit
# evidence only: no routing, rewards, slashing, or payout logic reads this table.
# Future validator economics can derive scorecards from this append-only evidence
# after assignment/quorum/dispute rules exist.
validator_attestations = sa.Table(
    "grid_validator_attestations",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    # sha256(canonical payload + signature) — idempotency for retried submits.
    sa.Column("attestation_hash", sa.String(64), nullable=False, unique=True),
    sa.Column(
        "account_id",
        sa.Uuid,
        sa.ForeignKey("grid_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    sa.Column("validator_wallet", sa.String(42), nullable=True, index=True),
    sa.Column(
        "assignment_id",
        sa.String(96),
        sa.ForeignKey("grid_validator_assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    sa.Column("grid_nonce", sa.String(128), nullable=True, index=True),
    sa.Column("evidence_hash", sa.String(64), nullable=True, index=True),
    # preview = model-routed/local evidence; authoritative = verified Grid
    # assignment + nonce + target match. Scorecards that matter must filter on
    # authoritative; preview stays visible for rollout/debugging only.
    sa.Column("authority", sa.String(24), nullable=False, default="preview", index=True),
    sa.Column("quorum_status", sa.String(24), nullable=False, default="pending", index=True),
    # String rather than FK: V0 model-routed canaries may not know the worker,
    # and future assignments may use external worker ids before registry sync.
    sa.Column("worker_id", sa.String(64), nullable=True, index=True),
    sa.Column("model", sa.String(255), nullable=True, index=True),
    sa.Column("modality", sa.String(16), nullable=True),
    sa.Column("capability", sa.String(128), nullable=True),
    sa.Column("canary_kind", sa.String(64), nullable=True),
    sa.Column("nonce", sa.String(128), nullable=True),
    sa.Column("verdict", sa.String(16), nullable=False, index=True),
    sa.Column("score", sa.Float, nullable=True),
    sa.Column("latency_ms", sa.Integer, nullable=True),
    sa.Column("epoch", sa.String(64), nullable=True, index=True),
    sa.Column("signature", sa.String(132), nullable=True),
    sa.Column("signature_status", sa.String(32), nullable=False, default="unsigned"),
    sa.Column("payload", PortableJSON, nullable=False, default=dict),
    sa.Column("created", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
)
