# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native user tokens, bounded service clients, and price snapshots.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_service_clients",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("allowed_providers", sa.JSON(), nullable=False),
        sa.Column("google_audiences", sa.JSON(), nullable=False),
        sa.Column("per_request_micro", sa.BigInteger(), nullable=True),
        sa.Column("daily_micro", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_service_clients_active", "grid_service_clients", ["active"])

    op.create_table(
        "grid_service_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "service_id",
            sa.String(64),
            sa.ForeignKey("grid_service_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False, unique=True),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_service_events_service_id", "grid_service_events", ["service_id"])
    op.create_index("ix_grid_service_events_account_id", "grid_service_events", ["account_id"])
    op.create_index("ix_grid_service_events_event_type", "grid_service_events", ["event_type"])
    op.create_index("ix_grid_service_events_created", "grid_service_events", ["created"])

    with op.batch_alter_table("grid_api_keys") as batch:
        batch.add_column(
            sa.Column("key_kind", sa.String(16), nullable=False, server_default=sa.text("'user'")),
        )
        batch.add_column(sa.Column("service_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_grid_api_keys_service_id",
            "grid_service_clients",
            ["service_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_grid_api_keys_service_id", ["service_id"])
        batch.create_index("ix_grid_api_keys_expires_at", ["expires_at"])

    with op.batch_alter_table("grid_reservations") as batch:
        batch.add_column(sa.Column("input_per_mtok_micro", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("output_per_mtok_micro", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("discount_bps", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
        batch.add_column(sa.Column("service_id", sa.String(64), nullable=True))
        batch.create_index("ix_grid_reservations_service_id", ["service_id"])


def downgrade() -> None:
    with op.batch_alter_table("grid_reservations") as batch:
        batch.drop_index("ix_grid_reservations_service_id")
        batch.drop_column("service_id")
        batch.drop_column("discount_bps")
        batch.drop_column("output_per_mtok_micro")
        batch.drop_column("input_per_mtok_micro")
    with op.batch_alter_table("grid_api_keys") as batch:
        batch.drop_index("ix_grid_api_keys_expires_at")
        batch.drop_index("ix_grid_api_keys_service_id")
        batch.drop_constraint("fk_grid_api_keys_service_id", type_="foreignkey")
        batch.drop_column("expires_at")
        batch.drop_column("service_id")
        batch.drop_column("key_kind")
    op.drop_table("grid_service_events")
    op.drop_table("grid_service_clients")
