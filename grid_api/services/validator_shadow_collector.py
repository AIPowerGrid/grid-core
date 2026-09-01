# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Isolated Redis-outbox consumer for validator shadow observations."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Any

import redis.exceptions
import sqlalchemy as sa

from ..database import new_session
from ..redis_client import get_redis
from ..safe_logging import error_type
from ..v2.schema import validator_shadow_observations as observations_t
from ..v2.schema import validator_shadow_outcomes as outcomes_t
from ..v2.schema import validator_shadow_runs as runs_t
from . import validator_shadow as shadow
from .route_events import MAX_CANDIDATE_BYTES, MAX_CANDIDATES, STREAM_KEY

logger = logging.getLogger("grid_api.validator_shadow_collector")

CONSUMER_GROUP = "grid-validator-shadow"
LEADER_KEY = "grid:validator-shadow-collector:leader"
CLAIM_IDLE_MS = 30_000
ORPHAN_OUTCOME_SECONDS = 600
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return _aware(datetime.fromisoformat(str(value)))


async def ensure_consumer_group() -> None:
    try:
        await get_redis().xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def acquire_leadership(*, token: str, ttl_seconds: int) -> bool:
    redis = get_redis()
    if await redis.set(LEADER_KEY, token, nx=True, ex=ttl_seconds):
        return True
    current = await redis.get(LEADER_KEY)
    if current != token:
        return False
    return bool(await redis.expire(LEADER_KEY, ttl_seconds))


async def _run_for_time(value: datetime) -> dict[str, Any] | None:
    async with await new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(runs_t)
                    .where(
                        runs_t.c.started.isnot(None),
                        runs_t.c.started <= value,
                        runs_t.c.scheduled_end >= value,
                        runs_t.c.status == "running",
                    )
                    .order_by(runs_t.c.started.desc())
                    .limit(2),
                )
            )
            .mappings()
            .all()
        )
    if len(rows) > 1:
        raise shadow.ShadowConflict("overlapping shadow runs cannot consume route events")
    return dict(rows[0]) if rows else None


async def _observation_for_route(route_ref: str) -> dict[str, Any] | None:
    async with await new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(observations_t)
                    .where(observations_t.c.route_ref == route_ref)
                    .order_by(observations_t.c.observed_at.desc())
                    .limit(2),
                )
            )
            .mappings()
            .all()
        )
    if len(rows) > 1:
        raise shadow.ShadowConflict("route commitment exists in multiple shadow runs")
    return dict(rows[0]) if rows else None


async def _outcome_for_observation(observation_id: int) -> dict[str, Any] | None:
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(outcomes_t).where(outcomes_t.c.observation_id == observation_id),
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def process_event(fields: dict[str, str], *, now: datetime | None = None) -> str:
    """Process one outbox event. Returns ack, retry, or discard."""
    current = _aware(now or datetime.now(UTC))
    kind = str(fields.get("kind") or "")
    route_ref = str(fields.get("route_ref") or "")
    if not _HEX_64.fullmatch(route_ref):
        raise ValueError("route_ref must be a lowercase SHA-256 commitment")
    if kind == "route":
        observed_at = _parse_time(fields["observed_at"])
        run = await _run_for_time(observed_at)
        if not run:
            return "discard"
        encoded_candidates = str(fields["candidates"])
        if len(encoded_candidates.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("candidate snapshot is too large")
        candidates = json.loads(encoded_candidates)
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
            raise ValueError("candidate snapshot has an invalid size")
        await shadow.record_observation(
            run_id=str(run["id"]),
            route_ref=route_ref,
            task_class=str(fields["task_class"]),
            modality=str(fields["modality"]),
            requested_capability=str(fields["capability"]),
            candidates=candidates,
            actual_model=str(fields["actual_model"]),
            actual_worker_id=str(fields.get("actual_worker_id") or "") or None,
            observed_at=observed_at,
        )
        return "ack"

    if kind == "outcome":
        finished_at = _parse_time(fields["finished_at"])
        observation = await _observation_for_route(route_ref)
        if not observation:
            if (current - finished_at).total_seconds() < ORPHAN_OUTCOME_SECONDS:
                return "retry"
            run = await _run_for_time(finished_at)
            if run:
                await shadow.record_error(
                    run_id=str(run["id"]),
                    stage="outcome",
                    error_code="outcome_without_observation",
                    observed_at=finished_at,
                )
            return "discard"
        existing = await _outcome_for_observation(int(observation["id"]))
        supplied_worker = str(fields.get("actual_worker_id") or "")
        supplied_status = str(fields["terminal_status"])
        if existing:
            if str(existing.get("actual_worker_id") or "") == supplied_worker and str(existing["terminal_status"]) == supplied_status:
                return "ack"
            raise shadow.ShadowConflict("route attempt has contradictory terminal outcomes")
        duration_raw = str(fields.get("duration_ms") or "")
        if duration_raw and not 0 <= int(duration_raw) <= 86_400_000:
            raise ValueError("route duration is outside the bounded range")
        await shadow.record_outcome(
            observation_id=int(observation["id"]),
            actual_worker_id=str(fields.get("actual_worker_id") or "") or None,
            terminal_status=supplied_status,
            duration_ms=int(duration_raw) if duration_raw else None,
            finished_at=finished_at,
        )
        return "ack"

    return "discard"


async def _read_batch(*, consumer: str, block_ms: int = 1000) -> list[tuple[str, dict[str, str]]]:
    redis = get_redis()
    claimed = await redis.xautoclaim(
        STREAM_KEY,
        CONSUMER_GROUP,
        consumer,
        min_idle_time=CLAIM_IDLE_MS,
        start_id="0-0",
        count=100,
    )
    if claimed and claimed[1]:
        return list(claimed[1])
    rows = await redis.xreadgroup(
        CONSUMER_GROUP,
        consumer,
        {STREAM_KEY: ">"},
        count=100,
        block=block_ms,
    )
    return list(rows[0][1]) if rows else []


async def collect_once(*, consumer: str, block_ms: int = 1000) -> dict[str, int]:
    redis = get_redis()
    counts = {"acked": 0, "retried": 0, "failed": 0}
    for message_id, fields in await _read_batch(consumer=consumer, block_ms=block_ms):
        try:
            result = await process_event(fields)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, shadow.ShadowConflict) as exc:
            logger.warning("Invalid shadow event discarded error_type=%s", error_type(exc))
            await redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
            await redis.xdel(STREAM_KEY, message_id)
            counts["failed"] += 1
            counts["acked"] += 1
            continue
        except Exception as exc:
            logger.warning("Shadow event processing failed error_type=%s", error_type(exc))
            counts["failed"] += 1
            continue
        if result == "retry":
            counts["retried"] += 1
            continue
        await redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
        await redis.xdel(STREAM_KEY, message_id)
        counts["acked"] += 1
    return counts


async def _running_run(now: datetime) -> dict[str, Any] | None:
    async with await new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(runs_t)
                    .where(
                        runs_t.c.status == "running",
                        runs_t.c.started <= now,
                        runs_t.c.scheduled_end >= now,
                    )
                    .limit(2),
                )
            )
            .mappings()
            .all()
        )
    if len(rows) > 1:
        raise shadow.ShadowConflict("multiple running shadow runs are not allowed")
    return dict(rows[0]) if rows else None


async def run_loop(*, sample_seconds: int) -> None:
    import asyncio

    token = secrets.token_hex(16)
    consumer = f"collector-{token[:12]}"
    lease_ttl = max(120, int(sample_seconds) * 2)
    next_sample = 0.0
    initialized = False
    loop = asyncio.get_running_loop()
    while True:
        try:
            if not initialized:
                await ensure_consumer_group()
                initialized = True
            if not await acquire_leadership(token=token, ttl_seconds=lease_ttl):
                await asyncio.sleep(min(30, sample_seconds))
                continue
            await collect_once(consumer=consumer, block_ms=1000)
            if loop.time() < next_sample:
                continue
            now = datetime.now(UTC)
            run = await _running_run(now)
            if run:
                await shadow.record_capacity_sample(run_id=str(run["id"]), sampled_at=now)
            next_sample = loop.time() + int(sample_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Shadow collector pass failed error_type=%s", error_type(exc))
            await asyncio.sleep(min(30, sample_seconds))
