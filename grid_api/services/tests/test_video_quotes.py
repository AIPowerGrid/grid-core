# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL quote identity/capacity; synthetic recipes, sources and tariffs.

This does not accept signatures, settle USDC, or qualify public model use.
"""

import asyncio
import dataclasses
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import database
from grid_api.config import GridSettings
from grid_api.routers import videos
from grid_api.services import pricing, video_plans
from grid_api.services import video_quotes as quotes
from grid_api.services.tests import test_media_job_journal as journal_tests
from grid_api.services.tests import test_video_plans as plan_tests
from grid_api.services.tests.test_video_plans import request
from grid_api.v2.schema import account_aliases, metadata, video_payment_attempts, video_quotes

db = journal_tests.db
owner = journal_tests.owner
synthetic_dependencies = journal_tests.synthetic_dependencies
admission = plan_tests.admission
registry = plan_tests.registry
mp4 = plan_tests.mp4
PAY_TO = "0x" + "1" * 40
INTENT = "a" * 64
LIMITS = {"per_account_limit": 8, "global_limit": 256}


async def prepared(aid, *, key=None, mode="generated", video=None, intent=INTENT, **overrides):
    inputs = await videos.validate_video(request(mode, video=video, **overrides), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    key = str(key or uuid4())
    quote = quotes.prepare(account_id=aid, idempotency_key=key, request_hash=intent, plan=plan,
                           max_cost_micro=100_000, server_max_micro=100_000,
                           network="eip155:84532", pay_to=PAY_TO)
    return quote, key


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
async def test_sources_seed_graph_and_tariff_frozen_without_economic_or_storage_authority(db, admission, mp4, mode):
    aid = await owner(0)
    quote, key = await prepared(aid, mode=mode, video=mp4)
    row, created = await quotes.persist(quote, idempotency_key=key, **LIMITS)
    assert created
    view = quotes.view(row)
    assert view["state"] == "quoted" and view["payment_enabled"] is False
    assert view["seed"] == 0 and view["execution"]["frames"] == 9
    assert view["payment"]["amount_micro"] == 7500 and view["payment"]["asset"] == quotes.USDC["eip155:84532"]
    assert view["payment"]["valid_before"] == view["expires"] == view["created"] + 300
    assert len(view["authorization_nonce"]) == 66
    assert len(view["inputs"]["sources"]) == {"generated": 0, "audio": 1, "video": 2}[mode]
    assert "payload" not in view and "owner" not in view and "salt" not in view
    assert "synthetic red ball" not in repr(quote)
    assert "data:" not in quote.snapshot_json and key not in quote.snapshot_json
    async with await database.new_session() as session:
        for table in metadata.tables.values():
            if table.name in {"grid_reservations", "grid_media_jobs", "grid_ledger", "grid_credit_ledger", "grid_x402_payments"}:
                assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0
    # Caller-owned views cannot mutate the serialized snapshot.
    original = quote.snapshot_json
    quote.snapshot["plan"]["payload"]["seed"] = 42
    view["payment"]["amount_micro"] = 1
    assert quote.snapshot_json == original
    assert (await quotes.lookup(aid, quote_id=row["id"]))["snapshot"] == quote.snapshot


@pytest.mark.asyncio
async def test_replay_after_tariff_change_and_expiry_keeps_one_nonce_seed_and_plan(db, admission, monkeypatch):
    aid = await owner(0)
    quote, key = await prepared(aid)
    first, _ = await quotes.persist(quote, idempotency_key=key, **LIMITS)
    original = quotes.view(first)
    monkeypatch.setitem(pricing.PRICING, quote.snapshot["plan"]["model"].lower(), pricing.ModelPrice(0, 0, video_per_second=0.01))
    monkeypatch.setattr(pricing, "PRICE_BOOK_VERSION", "synthetic-new-book")
    different, _ = await prepared(aid, key=key, seed=42)
    repeated, created = await quotes.persist(different, idempotency_key=key, per_account_limit=1, global_limit=1)
    assert not created and quotes.view(repeated) == original
    monkeypatch.setattr(quotes, "_now", lambda: first["expires"] + timedelta(seconds=1))
    recovered = await quotes.lookup(aid, idempotency_key=key)
    assert quotes.view(recovered) == {**original, "state": "expired"}
    again, created = await quotes.persist(different, idempotency_key=key, per_account_limit=1, global_limit=1)
    assert not created and again["commitment"] == first["commitment"]


@pytest.mark.asyncio
async def test_legacy_domainless_quote_recovers_without_upgrade_or_new_nonce(db, admission):
    aid = await owner(0)
    current, key = await prepared(aid)
    legacy = current.snapshot
    legacy["schema"] = "aipg.video-quote-binding.v1"
    legacy["payment"].pop("eip712_domain")
    old = quotes.PreparedVideoQuote(quotes._canonical(legacy))
    saved, _ = await quotes.persist(old, idempotency_key=key, **LIMITS)
    replay, created = await quotes.persist(current, idempotency_key=key, **LIMITS)
    assert not created and replay["snapshot"] == saved["snapshot"] == legacy
    assert replay["authorization_nonce"] == saved["authorization_nonce"]
    assert "eip712_domain" not in quotes.view(await quotes.lookup(aid, idempotency_key=key))["payment"]


@pytest.mark.asyncio
async def test_changed_intent_conflicts_and_foreign_selectors_are_hidden(db, admission):
    aid, stranger = await owner(0), await owner(0)
    quote, key = await prepared(aid)
    row, _ = await quotes.persist(quote, idempotency_key=key, **LIMITS)
    changed, _ = await prepared(aid, key=key, intent="b" * 64)
    with pytest.raises(quotes.VideoQuoteConflict):
        await quotes.persist(changed, idempotency_key=key, **LIMITS)
    for selector in ({"quote_id": row["id"]}, {"idempotency_key": key}, {"quote_id": uuid4()}):
        assert await quotes.lookup(stranger, **selector) is None


@pytest.mark.asyncio
async def test_expired_rows_keep_identity_and_consume_retained_capacity(db, admission, monkeypatch):
    aid = await owner(0)
    quote, key = await prepared(aid)
    row, _ = await quotes.persist(quote, idempotency_key=key, per_account_limit=1, global_limit=1)
    monkeypatch.setattr(quotes, "_now", lambda: row["expires"] + timedelta(seconds=1))
    fresh, fresh_key = await prepared(aid)
    with pytest.raises(quotes.VideoQuoteCapacity):
        await quotes.persist(fresh, idempotency_key=fresh_key, per_account_limit=1, global_limit=1)
    assert (await quotes.lookup(aid, idempotency_key=key))["commitment"] == row["commitment"]


@pytest.mark.asyncio
async def test_expiry_rechecked_after_wait_before_insert(db, admission, monkeypatch):
    aid = await owner(0)
    quote, key = await prepared(aid)
    monkeypatch.setattr(quotes, "_now", lambda: quote.values["expires"])
    with pytest.raises(quotes.VideoQuoteError):
        await quotes.persist(quote, idempotency_key=key, **LIMITS)
    assert await quotes.lookup(aid, idempotency_key=key) is None


@pytest.mark.asyncio
async def test_pilot_deadline_rechecked_in_commit_transaction(db, admission):
    aid = await owner(0)
    quote, key = await prepared(aid)
    with pytest.raises(quotes.VideoQuoteError, match="pilot expired"):
        await quotes.persist(quote, idempotency_key=key,
                             admission_until=datetime.now(UTC) - timedelta(seconds=1), **LIMITS)
    assert await quotes.lookup(aid, idempotency_key=key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf"), 1])
async def test_unpriced_invalid_or_over_ceiling_rate_cannot_create_quote(db, admission, monkeypatch, rate):
    aid = await owner(0)
    monkeypatch.setitem(pricing.PRICING, journal_tests.MODEL.lower(), pricing.ModelPrice(0, 0, video_per_second=rate))
    with pytest.raises(quotes.VideoQuoteUnpriced):
        await prepared(aid)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(video_quotes)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "authorization_nonce", "idempotency_hash"])
async def test_database_uniqueness_is_final_guard(db, admission, field):
    aid = await owner(0)
    first, key = await prepared(aid)
    await quotes.persist(first, idempotency_key=key, **LIMITS)
    second, _ = await prepared(aid)
    values = second.values
    values[field] = first.values[field]
    async with await database.new_session() as session:
        with pytest.raises(sa.exc.IntegrityError):
            async with session.begin_nested():
                await session.execute(video_quotes.insert().values(**values))


@pytest.mark.asyncio
async def test_alias_recovery_retains_frozen_owner_and_merge_ambiguity_rejects(db, admission):
    aid, alias = await owner(0), await owner(0)
    key = str(uuid4())
    first, _ = await prepared(aid, key=key)
    second, _ = await prepared(alias, key=key)
    original, _ = await quotes.persist(first, idempotency_key=key, **LIMITS)
    await quotes.persist(second, idempotency_key=key, **LIMITS)
    async with await database.new_session() as session:
        await session.execute(account_aliases.insert().values(source_account_id=alias, canonical_account_id=aid,
                                                             merge_ref="synthetic:" + uuid4().hex))
        await session.commit()
    recovered = await quotes.lookup(aid, quote_id=second.values["id"])
    assert recovered["snapshot"]["owner"] == str(alias)
    with pytest.raises(quotes.VideoQuoteConflict):
        await quotes.lookup(aid, idempotency_key=key)
    assert (await quotes.lookup(alias, quote_id=original["id"]))["commitment"] == original["commitment"]


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [("amount_micro", 1), ("request_hash", "b" * 64),
                                         ("authorization_nonce", "0x" + "c" * 64), ("plan_hash", "d" * 64)])
async def test_corrupted_index_fields_fail_closed_on_read(db, admission, column, value):
    aid = await owner(0)
    quote, key = await prepared(aid)
    row, _ = await quotes.persist(quote, idempotency_key=key, **LIMITS)
    async with await database.new_session() as session:
        await session.execute(video_quotes.update().where(video_quotes.c.id == row["id"]).values(**{column: value}))
        await session.commit()
    with pytest.raises(quotes.VideoQuoteError):
        await quotes.lookup(aid, quote_id=row["id"])


@pytest.mark.asyncio
async def test_changed_snapshot_rejected_and_compiled_plan_rechecked(db, admission):
    aid = await owner(0)
    quote, key = await prepared(aid)
    row, _ = await quotes.persist(quote, idempotency_key=key, **LIMITS)
    snapshot = quote.snapshot
    snapshot["plan"]["payload"]["seed"] = 42
    async with await database.new_session() as session:
        await session.execute(video_quotes.update().where(video_quotes.c.id == row["id"]).values(snapshot=snapshot))
        await session.commit()
    with pytest.raises(quotes.VideoQuoteError):
        await quotes.lookup(aid, quote_id=row["id"])
    inputs = await videos.validate_video(request("generated"), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    damaged = dataclasses.replace(plan, snapshot_json=plan.snapshot_json.replace('"seed":0', '"seed":42'))
    with pytest.raises(HTTPException) as error:
        quotes.prepare(account_id=aid, idempotency_key=key, request_hash=INTENT, plan=damaged,
                       max_cost_micro=100_000, server_max_micro=100_000, network="eip155:84532", pay_to=PAY_TO)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_real_postgres_duplicate_and_conflict_races_choose_one_frozen_quote(db, admission):
    if db[1] != "postgres":
        pytest.skip("row/advisory-lock concurrency proof requires PostgreSQL")
    aid = await owner(0)
    key = str(uuid4())
    candidates = [await prepared(aid, key=key, seed=index) for index in range(20)]
    result = await asyncio.gather(*[quotes.persist(quote, idempotency_key=key, **LIMITS) for quote, _ in candidates])
    assert sum(created for _, created in result) == 1
    assert len({row["commitment"] for row, _ in result}) == 1
    assert len({row["authorization_nonce"] for row, _ in result}) == 1
    changed = [await prepared(aid, key=key, intent="b" * 64) for _ in range(5)]
    result = await asyncio.gather(*[quotes.persist(quote, idempotency_key=key, **LIMITS) for quote, _ in changed],
                                  return_exceptions=True)
    assert all(isinstance(item, quotes.VideoQuoteConflict) for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_owner", [True, False])
async def test_real_postgres_caps_hold_under_many_creators(db, admission, same_owner):
    if db[1] != "postgres":
        pytest.skip("capacity concurrency proof requires PostgreSQL")
    owners = [await owner(0) for _ in range(12)]
    candidates = [await prepared(owners[0] if same_owner else aid) for aid in owners]
    result = await asyncio.gather(*[quotes.persist(quote, idempotency_key=key,
        per_account_limit=2, global_limit=3) for quote, key in candidates], return_exceptions=True)
    accepted = [item for item in result if isinstance(item, tuple)]
    assert len(accepted) == (2 if same_owner else 3)
    assert all(isinstance(item, tuple | quotes.VideoQuoteCapacity) for item in result)


def test_preparation_caps_and_cancellation_cleanup():
    first, second, third = uuid4(), uuid4(), uuid4()
    with quotes.preparation_slot(first):
        with pytest.raises(quotes.VideoQuoteCapacity), quotes.preparation_slot(first):
            pass
        with quotes.preparation_slot(second):
            with pytest.raises(quotes.VideoQuoteCapacity), quotes.preparation_slot(third):
                pass
    assert not quotes._preparing
    with pytest.raises(asyncio.CancelledError), quotes.preparation_slot(first):
        raise asyncio.CancelledError
    assert not quotes._preparing


def test_quote_settings_are_dark_bounded_and_require_private_pilot():
    assert not GridSettings(_env_file=None).video_x402_quotes_enabled
    enabled = dict(video_x402_quotes_enabled=True, video_x402_quote_accounts=[uuid4()],
                   video_x402_quote_pay_to=PAY_TO, video_x402_quotes_until=datetime.now(UTC) + timedelta(hours=1))
    assert GridSettings(_env_file=None, **enabled).video_x402_quote_network == "eip155:84532"
    for change in ({"video_x402_quote_accounts": []}, {"video_x402_quote_pay_to": ""},
                   {"video_x402_quote_pay_to": "0x" + "0" * 40}, {"video_x402_quotes_until": None},
                   {"video_x402_quotes_until": datetime.now(UTC) + timedelta(days=2)},
                   {"video_x402_quotes_until": datetime.now(UTC) - timedelta(seconds=1)},
                   {"video_x402_quote_capacity": 1}, {"video_x402_quote_network": "eip155:1"}):
        with pytest.raises(ValueError):
            GridSettings(_env_file=None, **{**enabled, **change})


def migration():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0046_video_quotes.py"
    spec = importlib.util.spec_from_file_location("video_quote_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_migration_parity_empty_downgrade_and_refusal_after_quote():
    revision = migration()
    assert revision.down_revision == "0045"
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            metadata.create_all(connection, tables=[table for table in metadata.sorted_tables
                                                    if table not in {video_quotes, video_payment_attempts}])
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
                inspector = sa.inspect(connection)
                assert {c["name"]: c["nullable"] for c in inspector.get_columns(video_quotes.name)} == {
                    c.name: c.nullable for c in video_quotes.columns}
                for kind, inspect_fn in ((sa.CheckConstraint, inspector.get_check_constraints),
                                         (sa.UniqueConstraint, inspector.get_unique_constraints)):
                    assert {c["name"] for c in inspect_fn(video_quotes.name)} == {
                        c.name for c in video_quotes.constraints if isinstance(c, kind)}
                assert {c["referred_table"] for c in inspector.get_foreign_keys(video_quotes.name)} == {"grid_accounts"}
                revision.downgrade()
                revision.upgrade()
                aid = uuid4()
                connection.execute(metadata.tables["grid_accounts"].insert().values(id=aid, flags={}))
                connection.execute(video_quotes.insert().values(id=uuid4(), job_id=uuid4(), account_id=aid,
                    idempotency_hash="a" * 64, request_hash="b" * 64, plan_hash="c" * 64,
                    commitment="d" * 64, authorization_nonce="0x" + "e" * 64, snapshot={},
                    amount_micro=1, max_cost_micro=1, created=datetime.now(UTC),
                    expires=datetime.now(UTC) + timedelta(minutes=5)))
                with pytest.raises(RuntimeError, match="refuse to discard"):
                    revision.downgrade()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_real_postgres_migration_matches_schema_and_preserves_quote_history(db, admission):
    if db[1] != "postgres":
        pytest.skip("PostgreSQL DDL proof requires PostgreSQL")
    revision = migration()
    quote, key = await prepared(await owner(0))

    def apply_and_check(connection):
        assert connection.scalar(sa.select(sa.func.count()).select_from(video_payment_attempts)) == 0
        video_payment_attempts.drop(connection)
        video_quotes.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            inspector = sa.inspect(connection)
            assert {c["name"]: c["nullable"] for c in inspector.get_columns(video_quotes.name)} == {
                c.name: c.nullable for c in video_quotes.columns}
            for kind, inspect_fn in ((sa.CheckConstraint, inspector.get_check_constraints),
                                     (sa.UniqueConstraint, inspector.get_unique_constraints)):
                assert {c["name"] for c in inspect_fn(video_quotes.name)} == {
                    c.name for c in video_quotes.constraints if isinstance(c, kind)}
            revision.downgrade()
            revision.upgrade()
        video_payment_attempts.create(connection)

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(apply_and_check)
    await quotes.persist(quote, idempotency_key=key, **LIMITS)

    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="refuse to discard"):
            revision.downgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert (await quotes.lookup(quote.values["account_id"], idempotency_key=key))["commitment"] == quote.values["commitment"]
