# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQLite upgrade to 0047 matches the schema and round-trips."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from grid_api.v2.schema import oauth_refresh_tokens

TABLE = "grid_oauth_refresh_tokens"


def test_refresh_token_table_matches_schema_and_roundtrips(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    path = tmp_path / "refresh.db"
    monkeypatch.setenv("GRID_DB_URL", f"sqlite+aiosqlite:///{path}")
    # No ini file: alembic's env would otherwise run logging.fileConfig, which
    # disables every existing logger for the tests that follow.
    config = Config()
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "0046")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert TABLE not in sa.inspect(engine).get_table_names()
        command.upgrade(config, "0047")
        inspector = sa.inspect(engine)
        assert {c["name"]: c["nullable"] for c in inspector.get_columns(TABLE)} == {
            column.name: column.nullable for column in oauth_refresh_tokens.c
        }
        assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == ["token_hash"]
        assert {index["name"] for index in inspector.get_indexes(TABLE)} == {
            f"ix_{TABLE}_{column.name}" for column in oauth_refresh_tokens.c if column.index
        }
        assert {check["name"] for check in inspector.get_check_constraints(TABLE)} == {
            constraint.name for constraint in oauth_refresh_tokens.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys(TABLE)} == {
            "grid_oauth_clients", "grid_accounts",
        }
        command.downgrade(config, "0046")
        assert TABLE not in sa.inspect(engine).get_table_names()
        command.upgrade(config, "0047")
        assert TABLE in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
