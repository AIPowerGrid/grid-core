# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add dark OAuth 2.1 authorization state for the remote MCP resource.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    portable_json = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "grid_oauth_clients",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("redirect_uris", portable_json, nullable=False),
        sa.Column("application_type", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "application_type IN ('native', 'web')",
            name="ck_grid_oauth_clients_application_type",
        ),
    )
    op.create_index("ix_grid_oauth_clients_active", "grid_oauth_clients", ["active"])

    op.create_table(
        "grid_oauth_authorizations",
        sa.Column("request_hash", sa.String(64), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(96),
            sa.ForeignKey("grid_oauth_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("resource", sa.String(512), nullable=False),
        sa.Column("scopes", portable_json, nullable=False),
        sa.Column("state", sa.String(512), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("auth_method", sa.String(16), nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'consumed')",
            name="ck_grid_oauth_authorizations_status",
        ),
        sa.CheckConstraint(
            "expires_at > created",
            name="ck_grid_oauth_authorizations_expiry",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'consumed') OR "
            "(account_id IS NOT NULL AND code_hash IS NOT NULL)",
            name="ck_grid_oauth_authorizations_approval",
        ),
    )
    op.create_index(
        "ix_grid_oauth_authorizations_client_id",
        "grid_oauth_authorizations",
        ["client_id"],
    )
    op.create_index(
        "ix_grid_oauth_authorizations_status",
        "grid_oauth_authorizations",
        ["status"],
    )
    op.create_index(
        "ix_grid_oauth_authorizations_account_id",
        "grid_oauth_authorizations",
        ["account_id"],
    )
    op.create_index(
        "ix_grid_oauth_authorizations_expires_at",
        "grid_oauth_authorizations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("grid_oauth_authorizations")
    op.drop_table("grid_oauth_clients")
