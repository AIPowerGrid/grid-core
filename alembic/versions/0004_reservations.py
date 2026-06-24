# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""durable per-job reservation lifecycle

Adds grid_reservations: one 'held' row per chargeable job, written by the HTTP
request handler at reserve-time. The worker-WS handler — the authority that
reaches a terminal state for EVERY job whether or not the client stayed
connected — flips it held→settled exactly once and reconciles/refunds against
the actual grid-counted usage. Keyed by job_id so settlement never depends on
the HTTP response collector (closes the stranded-reservation gap).

Ships dark with the rest of the credit system (GRID_CHARGING_ENABLED=0): until
charging is on, settle_job only LOGS the would-charge and never moves money.

NOTE: prod grid runs create_all (checkfirst), so v2 metadata auto-creates this
on the next boot. This migration is the canonical path for alembic-managed DBs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_reservations",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.Uuid, nullable=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("reserved_micro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("prompt_toks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="held"),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_grid_reservations_status", "grid_reservations", ["status"])
    op.create_index("ix_grid_reservations_created", "grid_reservations", ["created"])


def downgrade() -> None:
    op.drop_index("ix_grid_reservations_status", table_name="grid_reservations")
    op.drop_index("ix_grid_reservations_created", table_name="grid_reservations")
    op.drop_table("grid_reservations")
