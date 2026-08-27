# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private budget authorization for compensated validator audits.

This module is deliberately not imported by the scheduler, worker transport,
or settlement runtime. It establishes the durable, concurrency-safe money
boundary that a later reviewed dispatch phase can call.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import new_session
from ..v2.schema import reservations as demand_reservations_t
from ..v2.schema import validator_audit_budget_counters as counters_t
from ..v2.schema import validator_audit_jobs as audits_t
from ..v2.schema import validators as validators_t
from ..v2.schema import workers as workers_t
from .validators import VALIDATOR_HEARTBEAT_FRESH_SECONDS

MAX_UNITS = 9_000_000_000_000_000_000
MAX_TTL_SECONDS = 24 * 60 * 60
_AUDIT_ID_RE = re.compile(r"^aud_[A-Za-z0-9_-]{8,88}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MODALITIES = {"text", "image", "video", "audio"}
_ACTIVE = {"held", "queued", "running"}


class AuditBudgetError(ValueError):
    """Raised when an audit authorization or lifecycle transition is unsafe."""


class AuditBudgetExceeded(AuditBudgetError):
    """Raised when any independent hourly audit budget is exhausted."""


@dataclass(frozen=True)
class AuditBudgetLimits:
    global_hourly: int
    worker_hourly: int
    validator_hourly: int
    pair_hourly: int

    def validate(self) -> None:
        for name, value in (
            ("global_hourly", self.global_hourly),
            ("worker_hourly", self.worker_hourly),
            ("validator_hourly", self.validator_hourly),
            ("pair_hourly", self.pair_hourly),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_UNITS:
                raise AuditBudgetError(f"{name} must be a positive bounded integer")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _hour(value: datetime) -> datetime:
    current = _aware(value)
    if current is None:
        raise AuditBudgetError("current time is required")
    return current.replace(minute=0, second=0, microsecond=0)


def _bounded_units(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_UNITS:
        raise AuditBudgetError(f"{name} must be a positive bounded integer")
    return value


def _bounded_text(value: str, name: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise AuditBudgetError(f"{name} is required and must be at most {limit} characters")
    return normalized


def _sha256(value: str, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise AuditBudgetError(f"{name} must be a lowercase SHA-256 digest")
    return normalized


def _as_uuid(value: uuid.UUID | str, name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuditBudgetError(f"{name} must be a UUID") from exc


def _scope_contract(
    *,
    validator_id: str,
    worker_id: uuid.UUID,
    limits: AuditBudgetLimits,
) -> dict[str, tuple[str, int]]:
    pair_key = hashlib.sha256(f"{validator_id}:{worker_id}".encode()).hexdigest()
    return {
        "global": ("all", limits.global_hourly),
        "worker": (str(worker_id), limits.worker_hourly),
        "validator": (validator_id, limits.validator_hourly),
        "pair": (pair_key, limits.pair_hourly),
    }


def _counter_specs(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    pair_key = hashlib.sha256(
        f"{row['validator_id']}:{row['target_worker_id']}".encode(),
    ).hexdigest()
    keys = {
        "global": "all",
        "worker": str(row["target_worker_id"]),
        "validator": str(row["validator_id"]),
        "pair": pair_key,
    }
    return sorted(keys.items())


async def _job_lock(session: AsyncSession, job_id: uuid.UUID) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(
        hashlib.sha256(f"validator-audit:{job_id}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _counter_insert(session: AsyncSession):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(counters_t)
    if dialect == "sqlite":
        return sqlite_insert(counters_t)
    raise AuditBudgetError(f"unsupported audit-budget database dialect: {dialect}")


def _same_contract(
    row: Mapping[str, Any],
    *,
    audit_id: str,
    validator_id: str,
    worker_id: uuid.UUID,
    worker_name: str,
    model: str,
    modality: str,
    policy_id: str,
    corpus_id: str,
    reserved_units: int,
    request_hash: str,
) -> bool:
    return all(
        (
            row["id"] == audit_id,
            row["validator_id"] == validator_id,
            row["target_worker_id"] == worker_id,
            row["target_worker_name"] == worker_name,
            row["model"] == model,
            row["modality"] == modality,
            row["policy_id"] == policy_id,
            row["corpus_id"] == corpus_id,
            int(row["reserved_units"]) == reserved_units,
            row["request_hash"] == request_hash,
        ),
    )


async def reserve_audit(
    *,
    audit_id: str,
    job_id: uuid.UUID | str,
    validator_id: str,
    target_worker_id: uuid.UUID | str,
    target_worker_name: str,
    model: str,
    modality: str,
    policy_id: str,
    corpus_id: str,
    request_hash: str,
    reserved_units: int,
    limits: AuditBudgetLimits,
    allowed_signing_wallets: set[str] | frozenset[str],
    ttl_seconds: int = 3600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically reserve one private audit against all four hourly caps."""
    audit_id = _bounded_text(audit_id, "audit_id", 96)
    if not _AUDIT_ID_RE.fullmatch(audit_id):
        raise AuditBudgetError("audit_id must be an opaque aud_* identifier")
    job_uuid = _as_uuid(job_id, "job_id")
    worker_uuid = _as_uuid(target_worker_id, "target_worker_id")
    validator_id = _bounded_text(validator_id, "validator_id", 96)
    worker_name = _bounded_text(target_worker_name, "target_worker_name", 120)
    model = _bounded_text(model, "model", 255)
    modality = _bounded_text(modality, "modality", 16).lower()
    if modality not in _MODALITIES:
        raise AuditBudgetError("modality is not supported")
    policy_id = _bounded_text(policy_id, "policy_id", 128)
    corpus_id = _bounded_text(corpus_id, "corpus_id", 128)
    request_hash = _sha256(request_hash, "request_hash")
    reserved_units = _bounded_units(reserved_units, "reserved_units")
    limits.validate()
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise AuditBudgetError("ttl_seconds must be between 1 and 86400")
    allowlist = {str(wallet).strip().lower() for wallet in allowed_signing_wallets if wallet}
    if not allowlist:
        raise AuditBudgetError("allowed_signing_wallets must not be empty")

    current = _aware(now or _now())
    assert current is not None
    bucket = _hour(current)
    scopes = _scope_contract(
        validator_id=validator_id,
        worker_id=worker_uuid,
        limits=limits,
    )

    async with await new_session() as session:
        try:
            await _job_lock(session, job_uuid)
            existing = (
                (
                    await session.execute(
                        sa.select(audits_t)
                        .where(audits_t.c.job_id == job_uuid)
                        .with_for_update(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                if not _same_contract(
                    existing,
                    audit_id=audit_id,
                    validator_id=validator_id,
                    worker_id=worker_uuid,
                    worker_name=worker_name,
                    model=model,
                    modality=modality,
                    policy_id=policy_id,
                    corpus_id=corpus_id,
                    reserved_units=reserved_units,
                    request_hash=request_hash,
                ):
                    raise AuditBudgetError("job_id is already bound to a different audit contract")
                await session.commit()
                return {"status": "existing", "audit": dict(existing)}

            if await session.scalar(
                sa.select(sa.literal(True)).where(
                    sa.exists(
                        sa.select(demand_reservations_t.c.job_id).where(
                            demand_reservations_t.c.job_id == str(job_uuid),
                        ),
                    ),
                ),
            ):
                raise AuditBudgetError("job_id already has a demand-side reservation")

            validator = (
                (
                    await session.execute(
                        sa.select(validators_t)
                        .where(validators_t.c.id == validator_id)
                        .with_for_update(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not validator:
                raise AuditBudgetError("validator does not exist")
            reviewed_at = _aware(validator["independence_reviewed_at"])
            review_expires = _aware(validator["independence_expires_at"])
            heartbeat = _aware(validator["last_heartbeat"])
            if not all(
                (
                    validator["status"] == "active",
                    validator["independence_status"] == "verified",
                    bool(validator["operator_group_id"]),
                    bool(reviewed_at),
                    bool(review_expires and review_expires >= current),
                    bool(
                        heartbeat
                        and heartbeat
                        >= current - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS),
                    ),
                    str(validator["signing_wallet"]).lower() in allowlist,
                ),
            ):
                raise AuditBudgetError("validator is not currently allowlisted and independent")

            worker = (
                (
                    await session.execute(
                        sa.select(workers_t).where(workers_t.c.id == worker_uuid).with_for_update(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not worker:
                raise AuditBudgetError("target worker does not exist")
            if worker["name"] != worker_name:
                raise AuditBudgetError("target worker name snapshot does not match")
            if worker["maintenance"]:
                raise AuditBudgetError("target worker is in maintenance mode")
            if worker["type"] != modality:
                raise AuditBudgetError("target worker modality snapshot does not match")
            if model not in list(worker["models"] or []):
                raise AuditBudgetError("target worker does not advertise the requested model")

            for scope, (scope_key, cap) in sorted(scopes.items()):
                stmt = _counter_insert(session).values(
                    bucket_start=bucket,
                    scope=scope,
                    scope_key=scope_key,
                    cap_units=cap,
                    reserved_units=0,
                    spent_units=0,
                    created=current,
                    updated=current,
                )
                await session.execute(
                    stmt.on_conflict_do_nothing(
                        index_elements=["bucket_start", "scope", "scope_key"],
                    ),
                )

            for scope, (scope_key, _configured_cap) in sorted(scopes.items()):
                counter = (
                    (
                        await session.execute(
                            sa.select(counters_t)
                            .where(
                                counters_t.c.bucket_start == bucket,
                                counters_t.c.scope == scope,
                                counters_t.c.scope_key == scope_key,
                            )
                            .with_for_update(),
                        )
                    )
                    .mappings()
                    .one()
                )
                if int(counter["reserved_units"]) + int(counter["spent_units"]) + reserved_units > int(counter["cap_units"]):
                    raise AuditBudgetExceeded(f"{scope} hourly audit budget exhausted")

            for scope, (scope_key, _cap) in sorted(scopes.items()):
                await session.execute(
                    sa.update(counters_t)
                    .where(
                        counters_t.c.bucket_start == bucket,
                        counters_t.c.scope == scope,
                        counters_t.c.scope_key == scope_key,
                    )
                    .values(
                        reserved_units=counters_t.c.reserved_units + reserved_units,
                        updated=current,
                    ),
                )

            await session.execute(
                sa.insert(audits_t).values(
                    id=audit_id,
                    job_id=job_uuid,
                    validator_id=validator_id,
                    target_worker_id=worker_uuid,
                    target_worker_name=worker_name,
                    model=model,
                    modality=modality,
                    policy_id=policy_id,
                    corpus_id=corpus_id,
                    budget_bucket_start=bucket,
                    reserved_units=reserved_units,
                    actual_units=None,
                    request_hash=request_hash,
                    result_hash=None,
                    status="held",
                    failure_code=None,
                    created=current,
                    expires=current + timedelta(seconds=ttl_seconds),
                    queued_at=None,
                    started_at=None,
                    terminal_at=None,
                    updated=current,
                ),
            )
            await session.commit()
            return {
                "status": "reserved",
                "audit": {
                    "id": audit_id,
                    "job_id": job_uuid,
                    "status": "held",
                    "reserved_units": reserved_units,
                    "budget_bucket_start": bucket,
                },
            }
        except Exception:
            await session.rollback()
            raise


async def _locked_audit(session: AsyncSession, job_uuid: uuid.UUID):
    await _job_lock(session, job_uuid)
    return (
        (
            await session.execute(
                sa.select(audits_t).where(audits_t.c.job_id == job_uuid).with_for_update(),
            )
        )
        .mappings()
        .one_or_none()
    )


async def _locked_counters(session: AsyncSession, row: Mapping[str, Any]):
    locked = []
    for scope, scope_key in _counter_specs(row):
        counter = (
            (
                await session.execute(
                    sa.select(counters_t)
                    .where(
                        counters_t.c.bucket_start == row["budget_bucket_start"],
                        counters_t.c.scope == scope,
                        counters_t.c.scope_key == scope_key,
                    )
                    .with_for_update(),
                )
            )
            .mappings()
            .one_or_none()
        )
        if not counter:
            raise AuditBudgetError("audit budget counter is missing")
        locked.append(counter)
    return locked


async def settle_audit_in_session(
    session: AsyncSession,
    *,
    job_id: uuid.UUID | str,
    actual_units: int,
    result_hash: str,
    now: datetime | None = None,
) -> str:
    """Settle an audit inside the caller's transaction; never commits."""
    job_uuid = _as_uuid(job_id, "job_id")
    actual_units = _bounded_units(actual_units, "actual_units")
    result_hash = _sha256(result_hash, "result_hash")
    current = _aware(now or _now())
    assert current is not None

    async with session.begin_nested():
        row = await _locked_audit(session, job_uuid)
        if not row:
            return "no_audit"
        if row["status"] == "settled":
            if int(row["actual_units"]) == actual_units and row["result_hash"] == result_hash:
                return "duplicate"
            raise AuditBudgetError("audit is already settled with a different terminal result")
        if row["status"] in {"released", "manual_review"}:
            return "stale_no_payout"
        if row["status"] not in _ACTIVE:
            raise AuditBudgetError("audit is not settleable")
        reserved = int(row["reserved_units"])
        if actual_units > reserved:
            raise AuditBudgetError("actual_units exceeds the audit reservation")
        counters = await _locked_counters(session, row)
        if any(int(counter["reserved_units"]) < reserved for counter in counters):
            raise AuditBudgetError("audit budget counter is inconsistent")

        for scope, scope_key in _counter_specs(row):
            result = await session.execute(
                sa.update(counters_t)
                .where(
                    counters_t.c.bucket_start == row["budget_bucket_start"],
                    counters_t.c.scope == scope,
                    counters_t.c.scope_key == scope_key,
                    counters_t.c.reserved_units >= reserved,
                )
                .values(
                    reserved_units=counters_t.c.reserved_units - reserved,
                    spent_units=counters_t.c.spent_units + actual_units,
                    updated=current,
                ),
            )
            if result.rowcount != 1:
                raise AuditBudgetError("audit budget settlement lost its counter lock")
        await session.execute(
            sa.update(audits_t)
            .where(audits_t.c.job_id == job_uuid, audits_t.c.status.in_(_ACTIVE))
            .values(
                status="settled",
                actual_units=actual_units,
                result_hash=result_hash,
                terminal_at=current,
                updated=current,
            ),
        )
        return "settled"


async def release_audit(
    *,
    job_id: uuid.UUID | str,
    failure_code: str,
    now: datetime | None = None,
) -> str:
    """Return an active audit hold exactly once in an owned transaction."""
    job_uuid = _as_uuid(job_id, "job_id")
    failure_code = _bounded_text(failure_code, "failure_code", 64)
    current = _aware(now or _now())
    assert current is not None
    async with await new_session() as session:
        try:
            row = await _locked_audit(session, job_uuid)
            if not row:
                await session.commit()
                return "no_audit"
            if row["status"] == "settled":
                await session.commit()
                return "settled"
            if row["status"] == "released":
                await session.commit()
                return "duplicate"
            if row["status"] == "manual_review":
                await session.commit()
                return "manual_review"
            if row["status"] not in _ACTIVE:
                raise AuditBudgetError("audit is not releasable")
            reserved = int(row["reserved_units"])
            counters = await _locked_counters(session, row)
            if any(int(counter["reserved_units"]) < reserved for counter in counters):
                raise AuditBudgetError("audit budget counter is inconsistent")
            for scope, scope_key in _counter_specs(row):
                result = await session.execute(
                    sa.update(counters_t)
                    .where(
                        counters_t.c.bucket_start == row["budget_bucket_start"],
                        counters_t.c.scope == scope,
                        counters_t.c.scope_key == scope_key,
                        counters_t.c.reserved_units >= reserved,
                    )
                    .values(
                        reserved_units=counters_t.c.reserved_units - reserved,
                        updated=current,
                    ),
                )
                if result.rowcount != 1:
                    raise AuditBudgetError("audit budget release lost its counter lock")
            await session.execute(
                sa.update(audits_t)
                .where(audits_t.c.job_id == job_uuid, audits_t.c.status.in_(_ACTIVE))
                .values(
                    status="released",
                    failure_code=failure_code,
                    terminal_at=current,
                    updated=current,
                ),
            )
            await session.commit()
            return "released"
        except Exception:
            await session.rollback()
            raise
