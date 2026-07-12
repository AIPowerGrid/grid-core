"""DB defaults for Grid-native inserts into the legacy waiting_prompts table.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _waiting_prompt_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("waiting_prompts"):
        return set()
    return {column["name"] for column in inspector.get_columns("waiting_prompts")}


def upgrade() -> None:
    # Gateway-only databases do not contain the retired Horde tables. Existing
    # production does, and direct SQLAlchemy Core inserts need DB defaults even
    # when an older process omits these NOT NULL fields.
    columns = _waiting_prompt_columns()
    if not columns:
        return
    with op.batch_alter_table("waiting_prompts") as batch:
        if "validated_backends" in columns:
            batch.alter_column(
                "validated_backends",
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=sa.text("false"),
            )
        if "extra_slow_workers" in columns:
            batch.alter_column(
                "extra_slow_workers",
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=sa.text("false"),
            )


def downgrade() -> None:
    columns = _waiting_prompt_columns()
    if not columns:
        return
    with op.batch_alter_table("waiting_prompts") as batch:
        if "validated_backends" in columns:
            batch.alter_column(
                "validated_backends",
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=None,
            )
        if "extra_slow_workers" in columns:
            batch.alter_column(
                "extra_slow_workers",
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=None,
            )
