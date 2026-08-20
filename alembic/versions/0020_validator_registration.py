# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add registered validator identities and bind evidence to them.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_validators",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("signing_wallet", sa.String(42), nullable=False),
        sa.Column("software_version", sa.String(64), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("registration_signature", sa.String(132), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "signing_wallet",
            name="uq_grid_validators_account_wallet",
        ),
        sa.UniqueConstraint("signing_wallet", name="uq_grid_validators_signing_wallet"),
    )
    op.create_index("ix_grid_validators_account_id", "grid_validators", ["account_id"])
    op.create_index("ix_grid_validators_signing_wallet", "grid_validators", ["signing_wallet"])
    op.create_index("ix_grid_validators_status", "grid_validators", ["status"])
    op.create_index("ix_grid_validators_last_heartbeat", "grid_validators", ["last_heartbeat"])

    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.add_column(sa.Column("validator_id", sa.String(96), nullable=True))
        batch.create_foreign_key(
            "fk_grid_validator_assignments_validator_id",
            "grid_validators",
            ["validator_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_grid_validator_assignments_validator_id",
            ["validator_id"],
        )

    with op.batch_alter_table("grid_validator_attestations") as batch:
        batch.add_column(sa.Column("validator_id", sa.String(96), nullable=True))
        batch.create_foreign_key(
            "fk_grid_validator_attestations_validator_id",
            "grid_validators",
            ["validator_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_grid_validator_attestations_validator_id",
            ["validator_id"],
        )
        batch.create_unique_constraint(
            "uq_grid_validator_attestations_assignment_validator",
            ["assignment_id", "validator_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("grid_validator_attestations") as batch:
        batch.drop_constraint(
            "uq_grid_validator_attestations_assignment_validator",
            type_="unique",
        )
        batch.drop_index("ix_grid_validator_attestations_validator_id")
        batch.drop_constraint(
            "fk_grid_validator_attestations_validator_id",
            type_="foreignkey",
        )
        batch.drop_column("validator_id")

    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.drop_index("ix_grid_validator_assignments_validator_id")
        batch.drop_constraint(
            "fk_grid_validator_assignments_validator_id",
            type_="foreignkey",
        )
        batch.drop_column("validator_id")

    op.drop_index("ix_grid_validators_last_heartbeat", table_name="grid_validators")
    op.drop_index("ix_grid_validators_status", table_name="grid_validators")
    op.drop_index("ix_grid_validators_signing_wallet", table_name="grid_validators")
    op.drop_index("ix_grid_validators_account_id", table_name="grid_validators")
    op.drop_table("grid_validators")
