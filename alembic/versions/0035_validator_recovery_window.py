# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Collect bounded recent heartbeat coverage without rewriting qualification.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validators") as batch:
        batch.add_column(sa.Column("heartbeat_window_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column(
            "heartbeat_window_samples", sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False, server_default="[]",
        ))


def downgrade() -> None:
    # Operational rollback keeps these columns; an explicit downgrade loses
    # recent observations but never rewrites the legacy qualification history.
    with op.batch_alter_table("grid_validators") as batch:
        batch.drop_column("heartbeat_window_samples")
        batch.drop_column("heartbeat_window_started_at")
