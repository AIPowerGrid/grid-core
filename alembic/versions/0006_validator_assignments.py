# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""validator assignments and authoritative evidence gates

Adds Grid-issued validator assignments and the attestation columns needed to
separate preview evidence from assignment-bound authoritative evidence. This
migration is deliberately tolerant of early environments that may already have
the V0 attestation table from create_all/manual rollout.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

PortableJSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_index_once(name: str, table: str, cols: list[str], *, unique: bool = False) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, cols, unique=unique)


def _add_column_once(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _create_attestations_if_missing() -> None:
    if _has_table("grid_validator_attestations"):
        return
    op.create_table(
        "grid_validator_attestations",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("attestation_hash", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("validator_wallet", sa.String(42), nullable=True),
        sa.Column(
            "assignment_id",
            sa.String(96),
            sa.ForeignKey("grid_validator_assignments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("grid_nonce", sa.String(128), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("authority", sa.String(24), nullable=False, server_default="preview"),
        sa.Column("quorum_status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("capability", sa.String(128), nullable=True),
        sa.Column("canary_kind", sa.String(64), nullable=True),
        sa.Column("nonce", sa.String(128), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("epoch", sa.String(64), nullable=True),
        sa.Column("signature", sa.String(132), nullable=True),
        sa.Column("signature_status", sa.String(32), nullable=False, server_default="unsigned"),
        sa.Column("payload", PortableJSON, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attestation_hash", name="uq_grid_validator_attestation_hash"),
    )


def upgrade() -> None:
    if not _has_table("grid_validator_assignments"):
        op.create_table(
            "grid_validator_assignments",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("validator_wallet", sa.String(42), nullable=True),
            sa.Column("grid_nonce", sa.String(128), nullable=False),
            sa.Column("target_worker_id", sa.String(64), nullable=False),
            sa.Column("target_worker_name", sa.String(120), nullable=False),
            sa.Column("model", sa.String(255), nullable=False),
            sa.Column("modality", sa.String(16), nullable=False, server_default="text"),
            sa.Column("capability", sa.String(128), nullable=False),
            sa.Column("canary_kind", sa.String(64), nullable=False),
            sa.Column("scoring_policy_id", sa.String(128), nullable=False),
            sa.Column("challenge", PortableJSON, nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
            sa.Column("quorum_status", sa.String(24), nullable=False, server_default="pending"),
            sa.Column("quorum_outcome", sa.String(24), nullable=True),
            sa.Column("probe_job_id", sa.String(96), nullable=True),
            sa.Column("probe_status", sa.String(24), nullable=False, server_default="not_started"),
            sa.Column("probe_prompt_hash", sa.String(64), nullable=True),
            sa.Column("probe_response_hash", sa.String(64), nullable=True),
            sa.Column("probe_evidence_hash", sa.String(64), nullable=True),
            sa.Column("probe_verdict", sa.String(16), nullable=True),
            sa.Column("probe_latency_ms", sa.Integer, nullable=True),
            sa.Column("created", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
            sa.Column("probed", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finalized", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("grid_nonce", name="uq_grid_validator_assignments_nonce"),
        )
    _add_column_once("grid_validator_assignments", sa.Column("probe_prompt_hash", sa.String(64), nullable=True))
    _add_column_once("grid_validator_assignments", sa.Column("probe_response_hash", sa.String(64), nullable=True))
    _add_column_once("grid_validator_assignments", sa.Column("probe_evidence_hash", sa.String(64), nullable=True))
    _add_column_once("grid_validator_assignments", sa.Column("probe_verdict", sa.String(16), nullable=True))
    _add_column_once("grid_validator_assignments", sa.Column("probe_latency_ms", sa.Integer, nullable=True))
    _create_index_once("ix_grid_validator_assignments_account_id", "grid_validator_assignments", ["account_id"])
    _create_index_once("ix_grid_validator_assignments_validator_wallet", "grid_validator_assignments", ["validator_wallet"])
    _create_index_once("ix_grid_validator_assignments_target_worker_id", "grid_validator_assignments", ["target_worker_id"])
    _create_index_once("ix_grid_validator_assignments_target_worker_name", "grid_validator_assignments", ["target_worker_name"])
    _create_index_once("ix_grid_validator_assignments_model", "grid_validator_assignments", ["model"])
    _create_index_once("ix_grid_validator_assignments_status", "grid_validator_assignments", ["status"])
    _create_index_once("ix_grid_validator_assignments_quorum_status", "grid_validator_assignments", ["quorum_status"])
    _create_index_once("ix_grid_validator_assignments_probe_job_id", "grid_validator_assignments", ["probe_job_id"])
    _create_index_once("ix_grid_validator_assignments_probe_evidence_hash", "grid_validator_assignments", ["probe_evidence_hash"])
    _create_index_once("ix_grid_validator_assignments_created", "grid_validator_assignments", ["created"])
    _create_index_once("ix_grid_validator_assignments_expires", "grid_validator_assignments", ["expires"])

    _create_attestations_if_missing()
    _add_column_once("grid_validator_attestations", sa.Column("assignment_id", sa.String(96), nullable=True))
    _add_column_once("grid_validator_attestations", sa.Column("grid_nonce", sa.String(128), nullable=True))
    _add_column_once("grid_validator_attestations", sa.Column("evidence_hash", sa.String(64), nullable=True))
    _add_column_once(
        "grid_validator_attestations",
        sa.Column("authority", sa.String(24), nullable=False, server_default="preview"),
    )
    _add_column_once(
        "grid_validator_attestations",
        sa.Column("quorum_status", sa.String(24), nullable=False, server_default="pending"),
    )

    _create_index_once("ix_grid_validator_attestations_account_id", "grid_validator_attestations", ["account_id"])
    _create_index_once("ix_grid_validator_attestations_validator_wallet", "grid_validator_attestations", ["validator_wallet"])
    _create_index_once("ix_grid_validator_attestations_assignment_id", "grid_validator_attestations", ["assignment_id"])
    _create_index_once("ix_grid_validator_attestations_grid_nonce", "grid_validator_attestations", ["grid_nonce"])
    _create_index_once("ix_grid_validator_attestations_evidence_hash", "grid_validator_attestations", ["evidence_hash"])
    _create_index_once("ix_grid_validator_attestations_authority", "grid_validator_attestations", ["authority"])
    _create_index_once("ix_grid_validator_attestations_quorum_status", "grid_validator_attestations", ["quorum_status"])
    _create_index_once("ix_grid_validator_attestations_worker_id", "grid_validator_attestations", ["worker_id"])
    _create_index_once("ix_grid_validator_attestations_model", "grid_validator_attestations", ["model"])
    _create_index_once("ix_grid_validator_attestations_verdict", "grid_validator_attestations", ["verdict"])
    _create_index_once("ix_grid_validator_attestations_epoch", "grid_validator_attestations", ["epoch"])
    _create_index_once("ix_grid_validator_attestations_created", "grid_validator_attestations", ["created"])


def downgrade() -> None:
    for name in (
        "ix_grid_validator_attestations_created",
        "ix_grid_validator_attestations_epoch",
        "ix_grid_validator_attestations_verdict",
        "ix_grid_validator_attestations_model",
        "ix_grid_validator_attestations_worker_id",
        "ix_grid_validator_attestations_quorum_status",
        "ix_grid_validator_attestations_authority",
        "ix_grid_validator_attestations_evidence_hash",
        "ix_grid_validator_attestations_grid_nonce",
        "ix_grid_validator_attestations_assignment_id",
        "ix_grid_validator_attestations_validator_wallet",
        "ix_grid_validator_attestations_account_id",
    ):
        if name in _indexes("grid_validator_attestations"):
            op.drop_index(name, table_name="grid_validator_attestations")
    # Keep the legacy V0 attestation table on downgrade; only remove the
    # assignment-era columns if they exist. This avoids destroying evidence from
    # early deployments that created the table outside Alembic.
    for col in ("quorum_status", "authority", "evidence_hash", "grid_nonce", "assignment_id"):
        if col in _columns("grid_validator_attestations"):
            op.drop_column("grid_validator_attestations", col)

    for name in (
        "ix_grid_validator_assignments_expires",
        "ix_grid_validator_assignments_created",
        "ix_grid_validator_assignments_probe_evidence_hash",
        "ix_grid_validator_assignments_probe_job_id",
        "ix_grid_validator_assignments_quorum_status",
        "ix_grid_validator_assignments_status",
        "ix_grid_validator_assignments_model",
        "ix_grid_validator_assignments_target_worker_name",
        "ix_grid_validator_assignments_target_worker_id",
        "ix_grid_validator_assignments_validator_wallet",
        "ix_grid_validator_assignments_account_id",
    ):
        if name in _indexes("grid_validator_assignments"):
            op.drop_index(name, table_name="grid_validator_assignments")
    if _has_table("grid_validator_assignments"):
        op.drop_table("grid_validator_assignments")
