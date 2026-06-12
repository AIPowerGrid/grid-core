# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async SQLAlchemy engine and lightweight table mappings.

Maps only the columns needed for text generation — does NOT import
Flask-SQLAlchemy's db.Model classes. Both Flask and FastAPI write to
the same PostgreSQL database.

Column definitions match the actual production schema (verified via
information_schema queries).
"""

from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

metadata = sa.MetaData()

# ── Lightweight table mappings (read/write subset of columns) ──

users_table = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("api_key", sa.String(128)),
    sa.Column("username", sa.String(100)),
    sa.Column("kudos", sa.BigInteger, default=0),
    sa.Column("concurrency", sa.Integer, default=30),
)

workers_table = sa.Table(
    "workers",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("name", sa.String(100)),
    sa.Column("worker_type", sa.String(30)),
    sa.Column("max_length", sa.Integer),
    sa.Column("max_context_length", sa.Integer),
    sa.Column("last_check_in", sa.DateTime),
    sa.Column("maintenance", sa.Boolean, default=False),
    sa.Column("paused", sa.Boolean, default=False),
    sa.Column("bridge_agent", sa.Text),
    sa.Column("threads", sa.Integer, default=1),
    sa.Column("nsfw", sa.Boolean, default=False),
    sa.Column("ipaddr", sa.String(39)),
    # ── Legacy appeasement ──
    # Every column below is NOT NULL in the Haidra schema with defaults that
    # exist only in the Flask ORM, so a direct insert must supply them all or
    # first-time registration dies. Dies with this table in v2 step 6.
    sa.Column("kudos", sa.BigInteger, default=0),
    sa.Column("contributions", sa.BigInteger, default=0),
    sa.Column("fulfilments", sa.Integer, default=0),
    sa.Column("aborted_jobs", sa.Integer, default=0),
    sa.Column("uncompleted_jobs", sa.Integer, default=0),
    sa.Column("uptime", sa.BigInteger, default=0),
    sa.Column("last_reward_uptime", sa.BigInteger, default=0),
    sa.Column("max_power", sa.Integer, default=8),
    sa.Column("extra_slow_worker", sa.Boolean, default=False),
    sa.Column("maintenance_msg", sa.String(300), default=""),
    sa.Column("allow_unsafe_ipaddr", sa.Boolean, default=False),
    sa.Column("max_pixels", sa.BigInteger, default=1_048_576),
    sa.Column("allow_img2img", sa.Boolean, default=True),
    sa.Column("allow_painting", sa.Boolean, default=False),
    sa.Column("allow_post_processing", sa.Boolean, default=False),
    sa.Column("allow_controlnet", sa.Boolean, default=False),
    sa.Column("allow_sdxl_controlnet", sa.Boolean, default=False),
    sa.Column("allow_lora", sa.Boolean, default=False),
    sa.Column("limit_max_steps", sa.Boolean, default=False),
)

# Values for every legacy NOT-NULL-no-DB-default column, used at insert.
LEGACY_WORKER_DEFAULTS = dict(
    kudos=0,
    contributions=0,
    fulfilments=0,
    aborted_jobs=0,
    uncompleted_jobs=0,
    uptime=0,
    last_reward_uptime=0,
    max_power=8,
    extra_slow_worker=False,
    maintenance_msg="",
    allow_unsafe_ipaddr=False,
    max_pixels=1_048_576,
    allow_img2img=True,
    allow_painting=False,
    allow_post_processing=False,
    allow_controlnet=False,
    allow_sdxl_controlnet=False,
    allow_lora=False,
    limit_max_steps=False,
)

worker_models_table = sa.Table(
    "worker_models",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("worker_id", UUID(as_uuid=True), sa.ForeignKey("workers.id")),
    sa.Column("model", sa.String(255)),
)

waiting_prompts_table = sa.Table(
    "waiting_prompts",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4),
    sa.Column("wp_type", sa.String(30)),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("prompt", sa.Text),
    sa.Column("params", JSONB),
    sa.Column("gen_payload", JSONB),
    sa.Column("n", sa.Integer, default=1),
    sa.Column("jobs", sa.Integer, default=0),
    sa.Column("things", sa.Integer, default=0),
    sa.Column("total_usage", sa.Float, default=0),
    sa.Column("job_ttl", sa.Integer, default=150),
    sa.Column("disable_batching", sa.Boolean, default=False),
    sa.Column("worker_blacklist", sa.Boolean, default=False),
    sa.Column("active", sa.Boolean, default=True),
    sa.Column("faulted", sa.Boolean, default=False),
    sa.Column("expiry", sa.DateTime),
    sa.Column("created", sa.DateTime, default=datetime.utcnow),
    sa.Column("consumed_kudos", sa.Float, default=0),
    sa.Column("kudos", sa.Float, default=0),
    sa.Column("max_length", sa.Integer),
    sa.Column("max_context_length", sa.Integer),
    sa.Column("nsfw", sa.Boolean, default=False),
    sa.Column("slow_workers", sa.Boolean, default=True),
    sa.Column("trusted_workers", sa.Boolean, default=False),
    sa.Column("ipaddr", sa.String(39)),
    sa.Column("safe_ip", sa.Boolean, default=True),
    sa.Column("webhook", sa.String(1024)),
    sa.Column("client_agent", sa.String(100)),
    sa.Column("extra_priority", sa.Float, default=0),
)

processing_gens_table = sa.Table(
    "processing_gens",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4),
    sa.Column("procgen_type", sa.String(30)),
    sa.Column("wp_id", UUID(as_uuid=True), sa.ForeignKey("waiting_prompts.id")),
    sa.Column("worker_id", UUID(as_uuid=True), sa.ForeignKey("workers.id")),
    sa.Column("model", sa.String(255)),
    sa.Column("generation", sa.Text),
    sa.Column("seed", sa.BigInteger),
    sa.Column("start_time", sa.DateTime, default=datetime.utcnow),
    sa.Column("created", sa.DateTime, default=datetime.utcnow),
    sa.Column("cancelled", sa.Boolean, default=False),
    sa.Column("faulted", sa.Boolean, default=False),
    sa.Column("fake", sa.Boolean, default=False),
    sa.Column("censored", sa.Boolean, default=False),
    sa.Column("gen_metadata", JSONB),
    sa.Column("job_ttl", sa.Integer, default=150),
    sa.Column("progress_percent", sa.Integer, default=0),
    sa.Column("current_step", sa.Integer, default=0),
    sa.Column("total_steps", sa.Integer, default=0),
    sa.Column("media_type", sa.String(30), default="text"),
)


# ── Den ledger ──
# Durable, append-only record of den (work units) earned per completed job.
# The on-chain settlement bot rolls this up by wallet (or by worker→user
# mapping) over a period and pays out AIPG. Before this table, den was
# computed and sent to the worker but never persisted — so the payout system
# had nothing to pay against. This is grid_api-owned (not part of the horde
# schema) and created idempotently in init_database().
den_events_table = sa.Table(
    "grid_den_events",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("job_id", UUID(as_uuid=True), index=True),
    sa.Column("worker_id", UUID(as_uuid=True), index=True),
    # Best-effort wallet captured at worker connect. May be empty; settlement
    # can fall back to resolving the worker's user→wallet at payout time.
    sa.Column("wallet_address", sa.String(64), index=True),
    sa.Column("model", sa.String(255)),
    sa.Column("den", sa.Float, default=0),
    sa.Column("output_tokens", sa.Integer, default=0),
    sa.Column("created", sa.DateTime, default=datetime.utcnow, index=True),
)


# ── Engine + session factory ──

_engine = None
_session_factory = None


async def init_database():
    """Initialize the async engine and session factory."""
    global _engine, _session_factory
    settings = get_settings()
    _engine = create_async_engine(
        settings.async_database_url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    # Create grid_api-owned tables idempotently. checkfirst + an explicit
    # table list means we only ever touch tables we own — never the horde
    # schema, which is managed separately. Safe to run on every boot.
    #
    # v2 tables: Alembic is the canonical migration path (alembic upgrade
    # head), but create_all here keeps a fresh boot working without a manual
    # step — identical DDL, checkfirst, grid_-namespaced only.
    from .v2.schema import metadata as v2_metadata

    async with _engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: metadata.create_all(
                sync_conn, tables=[den_events_table], checkfirst=True
            )
        )
        await conn.run_sync(lambda sync_conn: v2_metadata.create_all(sync_conn, checkfirst=True))


async def close_database():
    """Dispose of the engine connection pool."""
    global _engine
    if _engine:
        await _engine.dispose()


async def get_session() -> AsyncSession:
    """FastAPI dependency that yields an async session."""
    async with _session_factory() as session:
        yield session


async def new_session() -> AsyncSession:
    """Create a standalone session (for use outside of Depends)."""
    return _session_factory()
