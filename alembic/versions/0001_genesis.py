# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""v2 genesis — accounts, api_keys, workers, jobs, ledger, epochs

Revision ID: 0001
Revises:
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Self-contained copy of the portable JSON type (migrations must not import
# app code that can drift after this revision is written).
PortableJSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "grid_accounts",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("wallet", sa.String(42), unique=True, nullable=True, index=True),
        sa.Column("email", sa.String(254), unique=True, nullable=True),
        sa.Column("oauth_sub", sa.String(255), unique=True, nullable=True),
        sa.Column("username", sa.String(100), nullable=True),
        sa.Column("flags", PortableJSON, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "grid_api_keys",
        sa.Column("hash", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid,
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean, nullable=False),
    )

    op.create_table(
        "grid_workers",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid,
            sa.ForeignKey("grid_accounts.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.String(120), unique=True, nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("wallet", sa.String(42), nullable=True, index=True),
        sa.Column("models", PortableJSON, nullable=False),
        sa.Column("capabilities", PortableJSON, nullable=False),
        sa.Column("bridge_agent", sa.String(120), nullable=True),
        sa.Column("maintenance", sa.Boolean, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("jobs_completed", sa.BigInteger, nullable=False),
        sa.Column("den_earned", sa.Float, nullable=False),
    )

    op.create_table(
        "grid_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid,
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("payload", PortableJSON, nullable=False),
        sa.Column("result", PortableJSON, nullable=True),
        sa.Column("worker_id", sa.Uuid, sa.ForeignKey("grid_workers.id"), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("den", sa.Float, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "grid_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("epoch_id", sa.String(32), nullable=True, index=True),
        sa.Column("job_id", sa.Uuid, nullable=False, index=True),
        sa.Column("worker_id", sa.Uuid, nullable=False, index=True),
        sa.Column("wallet", sa.String(42), nullable=True, index=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("job_type", sa.String(16), nullable=False),
        sa.Column("den", sa.Float, nullable=False),
        sa.Column("output_units", sa.Integer, nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("worker_sig", sa.String(132), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, index=True),
    )

    op.create_table(
        "grid_epochs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("opened", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_den", sa.Float, nullable=False),
        sa.Column("merkle_root", sa.String(66), nullable=True),
        sa.Column("aipg_paid", sa.Numeric(38, 18), nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("finalized", sa.Boolean, nullable=False),
    )


def downgrade() -> None:
    for t in (
        "grid_epochs",
        "grid_ledger",
        "grid_jobs",
        "grid_workers",
        "grid_api_keys",
        "grid_accounts",
    ):
        op.drop_table(t)
