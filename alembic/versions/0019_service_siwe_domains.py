# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bind partner SIWE challenges to explicit service domains.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_service_clients") as batch:
        batch.add_column(
            sa.Column(
                "siwe_domains",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )
    # SQLite cannot emit ALTER COLUMN DROP DEFAULT. Batch mode rebuilds the
    # table there while remaining a normal alter on Postgres.
    with op.batch_alter_table("grid_service_clients") as batch:
        batch.alter_column(
            "siwe_domains",
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("grid_service_clients") as batch:
        batch.drop_column("siwe_domains")
