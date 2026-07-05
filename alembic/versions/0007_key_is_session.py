# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""api_keys.is_session — wallet-proven session flag for account-admin gating

Adds grid_api_keys.is_session (bool, default false). Account-admin actions
(change payout wallet, issue/revoke keys) require a session key, so a leaked
inference key cannot redirect earnings. Existing keys default to non-session;
back-fill the wallet-proven ones (wallet-login / dashboard-session labels) so
current dashboard users aren't locked out of managing their own keys.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "is_session" not in _columns("grid_api_keys"):
        op.add_column(
            "grid_api_keys",
            sa.Column("is_session", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    # Back-fill ONLY the unambiguously wallet-/dashboard-proven login keys. We do
    # NOT promote the generic 'default' label: an early `default` key may have been
    # copied into an app/worker as an inference credential, and marking it a session
    # key would let that leaked key manage payout wallet + keys — the exact hole
    # this closes. Accounts whose only key is a `default` key must re-login (SIWE
    # wallet-login or dashboard session) to obtain a session key; nothing is paid
    # or charged in the interim (both are dark), so this is safe pre-launch.
    op.execute(
        "UPDATE grid_api_keys SET is_session = true "
        "WHERE label IN ('wallet-login', 'dashboard-session')"
    )


def downgrade() -> None:
    if "is_session" in _columns("grid_api_keys"):
        op.drop_column("grid_api_keys", "is_session")
