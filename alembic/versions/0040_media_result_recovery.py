# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persist private media results with demand settlement.

Revision ID: 0040
Revises: 0039
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("grid_reservations", sa.Column("media_client_ref", sa.String(128), nullable=True))
    op.add_column("grid_reservations", sa.Column("media_result", sa.JSON(none_as_null=True), nullable=True))
    op.create_index("ix_grid_reservations_media_owner_ref", "grid_reservations",
                    ["account_id", "media_client_ref"])


def downgrade():
    # Rolling the application back is safe; deleting paid recovery evidence is not.
    count = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM grid_reservations "
        "WHERE media_client_ref IS NOT NULL OR media_result IS NOT NULL"
    )).scalar_one()
    if count:
        raise RuntimeError("Cannot discard media recovery records; retain migration 0040")
    with op.batch_alter_table("grid_reservations") as batch:
        batch.drop_index("ix_grid_reservations_media_owner_ref")
        batch.drop_column("media_result")
        batch.drop_column("media_client_ref")
