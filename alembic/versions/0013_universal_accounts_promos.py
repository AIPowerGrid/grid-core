# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""canonical identities, account aliases, scoped keys, and promotional grants

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _subject_hash(kind: str, subject: str) -> str:
    canonical = subject.strip().lower() if kind in {"wallet", "email"} else subject.strip()
    if kind in {"google", "github"} and canonical.lower().startswith(f"{kind}_"):
        canonical = canonical[len(kind) + 1:]
    return hashlib.sha256(f"{kind}:{canonical}".encode()).hexdigest()


def upgrade() -> None:
    op.create_table(
        "grid_account_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("display_hint", sa.String(254), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "subject_hash", name="uq_grid_identity_subject"),
    )
    op.create_index("ix_grid_account_identities_account_id", "grid_account_identities", ["account_id"])

    op.create_table(
        "grid_account_aliases",
        sa.Column("source_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("canonical_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("merge_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_account_aliases_canonical_account_id", "grid_account_aliases", ["canonical_account_id"])

    op.create_table(
        "grid_identity_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("actor_account_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("identity_kind", sa.String(24), nullable=True),
        sa.Column("subject_hash", sa.String(64), nullable=True),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_identity_events_account_id", "grid_identity_events", ["account_id"])
    op.create_index("ix_grid_identity_events_actor_account_id", "grid_identity_events", ["actor_account_id"])
    op.create_index("ix_grid_identity_events_event_type", "grid_identity_events", ["event_type"])
    op.create_index("ix_grid_identity_events_created", "grid_identity_events", ["created"])

    op.create_table(
        "grid_promo_campaigns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("grant_micro", sa.BigInteger(), nullable=False),
        sa.Column("budget_micro", sa.BigInteger(), nullable=True),
        sa.Column("granted_micro", sa.BigInteger(), nullable=False),
        sa.Column("expires_days", sa.Integer(), nullable=True),
        sa.Column("eligibility", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("starts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_promo_campaigns_active", "grid_promo_campaigns", ["active"])

    op.create_table(
        "grid_promo_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", sa.String(64), sa.ForeignKey("grid_promo_campaigns.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("remaining_micro", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False, unique=True),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "campaign_id", name="uq_grid_promo_account_campaign"),
    )
    op.create_index("ix_grid_promo_grants_account_id", "grid_promo_grants", ["account_id"])
    op.create_index("ix_grid_promo_grants_campaign_id", "grid_promo_grants", ["campaign_id"])
    op.create_index("ix_grid_promo_grants_status", "grid_promo_grants", ["status"])
    op.create_index("ix_grid_promo_grants_expires", "grid_promo_grants", ["expires"])

    op.create_table(
        "grid_promo_spends",
        sa.Column("ref", sa.String(128), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("kept_micro", sa.BigInteger(), nullable=False),
        sa.Column("allocations", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_promo_spends_account_id", "grid_promo_spends", ["account_id"])
    op.create_index("ix_grid_promo_spends_status", "grid_promo_spends", ["status"])

    with op.batch_alter_table("grid_api_keys") as batch:
        batch.add_column(sa.Column("scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table("grid_reservations") as batch:
        batch.add_column(sa.Column("promo_micro", sa.BigInteger(), nullable=False, server_default=sa.text("0")))

    now = datetime.now(timezone.utc)
    bind = op.get_bind()
    accounts = sa.table(
        "grid_accounts",
        sa.column("id", sa.Uuid()), sa.column("wallet", sa.String()),
        sa.column("email", sa.String()), sa.column("oauth_sub", sa.String()),
    )
    identities = sa.table(
        "grid_account_identities",
        sa.column("id", sa.Uuid()), sa.column("account_id", sa.Uuid()),
        sa.column("kind", sa.String()), sa.column("subject_hash", sa.String()),
        sa.column("display_hint", sa.String()), sa.column("metadata", sa.JSON()),
        sa.column("verified_at", sa.DateTime(timezone=True)), sa.column("is_primary", sa.Boolean()),
        sa.column("created", sa.DateTime(timezone=True)),
    )
    for row in bind.execute(sa.select(accounts)).mappings():
        oauth_kind = "github" if (row["oauth_sub"] or "").lower().startswith("github_") else "google"
        for kind, subject in (("wallet", row["wallet"]), ("email", row["email"]), (oauth_kind, row["oauth_sub"])):
            if not subject:
                continue
            hint = subject if kind == "wallet" else (subject[:3] + "…" if len(subject) > 3 else "linked")
            verified_at = now if kind in {"wallet", "google", "github"} else None
            bind.execute(sa.insert(identities).values(
                id=uuid.uuid4(), account_id=row["id"], kind=kind,
                subject_hash=_subject_hash(kind, subject), display_hint=hint,
                metadata={"source": "legacy", "verified": bool(verified_at)},
                verified_at=verified_at, is_primary=kind in {"wallet", "google", "github"}, created=now,
            ))

    campaigns = sa.table(
        "grid_promo_campaigns",
        sa.column("id", sa.String()), sa.column("name", sa.String()),
        sa.column("grant_micro", sa.BigInteger()), sa.column("budget_micro", sa.BigInteger()),
        sa.column("granted_micro", sa.BigInteger()), sa.column("expires_days", sa.Integer()),
        sa.column("eligibility", sa.JSON()), sa.column("active", sa.Boolean()),
        sa.column("created", sa.DateTime(timezone=True)),
    )
    bind.execute(sa.insert(campaigns).values(
        id="universal-welcome-v1", name="Universal welcome credit",
        grant_micro=150_000, budget_micro=15_000_000_000, granted_micro=0,
        expires_days=30, eligibility={"verified_google": True}, active=True,
        created=now,
    ))


def downgrade() -> None:
    with op.batch_alter_table("grid_reservations") as batch:
        if "promo_micro" in _columns("grid_reservations"):
            batch.drop_column("promo_micro")
    with op.batch_alter_table("grid_api_keys") as batch:
        if "scopes" in _columns("grid_api_keys"):
            batch.drop_column("scopes")
    op.drop_table("grid_promo_spends")
    op.drop_table("grid_promo_grants")
    op.drop_table("grid_promo_campaigns")
    op.drop_table("grid_identity_events")
    op.drop_table("grid_account_aliases")
    op.drop_table("grid_account_identities")
