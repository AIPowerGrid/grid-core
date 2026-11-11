# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rotating OAuth refresh tokens for public clients (hash-only, dark by default)."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade():
    portable_json = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "grid_oauth_refresh_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column(
            "client_id",
            sa.String(96),
            sa.ForeignKey("grid_oauth_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(512), nullable=False),
        sa.Column("scopes", portable_json, nullable=False),
        sa.Column("auth_method", sa.String(16), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(16), nullable=True),
        sa.CheckConstraint("expires_at > created", name="ck_grid_oauth_refresh_tokens_expiry"),
        sa.CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN ('client', 'user', 'reuse', 'merge')",
            name="ck_grid_oauth_refresh_tokens_revoke_reason",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)",
            name="ck_grid_oauth_refresh_tokens_revocation",
        ),
    )
    for column in ("family_id", "client_id", "account_id", "expires_at"):
        op.create_index(f"ix_grid_oauth_refresh_tokens_{column}", "grid_oauth_refresh_tokens", [column])


def downgrade():
    # Operational session state, not an economic record: dropping it signs
    # clients out and they authorize again.
    op.drop_table("grid_oauth_refresh_tokens")
