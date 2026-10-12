# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private journal and bounded outbox for default-off async video admission.

Reservations remain the sole money/result authority. Redis may redeliver, but a
SQL execution claim is irrevocable: uncertain execution is never rerendered.
Only the scoped video-jobs ingress may admit these governed credit-backed jobs.
Disabling new admission does not stop recovery or draining existing holds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import new_session
from ..v2.schema import accounts, media_jobs, reservations
from . import identities, job_queue, media_results, media_source_retention, validator_audit_budgets

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_DISPATCH_ATTEMPTS = 6
EXECUTION_LEASE_SECONDS = 60
EXECUTION_RENEW_SECONDS = 20
MAX_EXECUTION_SECONDS = 1800
MARKER = "_grid_media_job_version"
_NAMESPACE = UUID("291c2882-f497-4c23-b82f-f4731a0723d6")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_QUEUE_ID = re.compile(r"^[0-9]{1,20}-[0-9]{1,20}$")


class MediaJobError(ValueError):
    """Invalid or internally inconsistent journal state (no request values)."""


class MediaJobConflict(MediaJobError):
    """An identity was reused with a different request, or became ambiguous."""


class MediaJobPaymentRequired(MediaJobError):
    """No positive, live credit reservation could be committed."""


class MediaJobCapacityExceeded(MediaJobError):
    """The canonical account already holds its allowed async work."""


class MediaJobAdmissionClosed(MediaJobError):
    """A captured pilot cannot authorize a new hold after its deadline."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _uuid(value) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MediaJobError("valid UUID required") from exc


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MediaJobError("lowercase SHA-256 commitment required")
    return value


def _json(value) -> str:
    # The coordinator owns this snapshot. Refuse accidentally forwarded auth
    # fields and embedded inputs before storing customer data durably.
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if depth > 64 or seen > 20_000:
            raise MediaJobError("journal JSON exceeds structural limit")
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str) or key.lower().replace("-", "_") in {
                    "authorization", "api_key", "x_api_key", "grid_user_token", "user_token", "private_key", "service_secret",
                }:
                    raise MediaJobError("credential fields are forbidden in journal JSON")
                stack.append((nested, depth + 1))
        elif isinstance(item, list):
            stack.extend((nested, depth + 1) for nested in item)
        elif isinstance(item, str) and re.match(r"^data:[^,]*;base64,", item, re.IGNORECASE):
            raise MediaJobError("inline inputs are forbidden in compiled journal JSON")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MediaJobError("invalid journal JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise MediaJobError("journal JSON exceeds size limit")
    return encoded


def _key_hash(value: str) -> str:
    # Require an opaque UUID rather than retaining caller text or weak secrets.
    key = _uuid(value)
    if key.version != 4:
        raise MediaJobError("idempotency key must be a UUIDv4")
    return hashlib.sha256(f"aipg-media-key-v1:{key}".encode("ascii")).hexdigest()


def validate_idempotency_key(value: str) -> None:
    """Ingress validation before any source upload or request-count quota."""
    _key_hash(value)


def _payload_hash(payload: dict, route_models: list[str], model: str, preferred_worker: str) -> str:
    return hashlib.sha256(_json({"payload": payload, "models": route_models, "model": model,
                                "preferred_worker": preferred_worker, "job_type": "video"}).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PreparedMediaJob:
    """Server-compiled immutable snapshot; never construct from a client graph."""

    job_id: str
    account_id: UUID
    idempotency_hash: str
    request_hash: str
    payload_hash: str
    model: str
    payload_json: str
    route_json: str
    preferred_worker: str
    max_cost_micro: int
    seconds: float
    admission_deadline: datetime | None = None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)

    @property
    def route_models(self) -> list[str]:
        return json.loads(self.route_json)

    def validate_binding(self, account_id, model, job_type, n, seconds, job_id, max_cost_micro_usd):
        if (self.account_id != _uuid(account_id) or self.model != model or job_type != "video"
                or type(n) is not int or n != 1 or str(job_id) != self.job_id
                or type(max_cost_micro_usd) is not int or self.max_cost_micro != max_cost_micro_usd
                or seconds != self.seconds):
            raise MediaJobError("reservation does not match prepared video job")
        _validate_prepared(self)
        _require_admission_deadline(self)


def prepare(*, account_id, idempotency_key: str, request_hash: str, model: str,
            payload: dict, route_models: list[str], max_cost_micro: int,
            preferred_worker: str = "", admission_deadline: datetime | None = None) -> PreparedMediaJob:
    """Bind a previously validated request to one fully compiled governed graph.

    Fingerprint the normalized client intent BEFORE choosing an omitted seed or
    uploading inputs. The ingress must authenticate, prove worker affinity and
    validate/compile that intent; this private helper grants none of those rights.
    """
    owner = _uuid(account_id)
    key_hash = _key_hash(idempotency_key)
    frozen = {**payload, MARKER: 1}
    seconds = frozen.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds):
        raise MediaJobError("compiled video duration required")
    job = PreparedMediaJob(
        job_id=str(uuid5(_NAMESPACE, f"{owner}:{key_hash}")), account_id=owner,
        idempotency_hash=key_hash, request_hash=_digest(request_hash),
        payload_hash=_payload_hash(frozen, route_models, model, preferred_worker),
        model=model, payload_json=_json(frozen), route_json=_json(route_models),
        preferred_worker=preferred_worker, max_cost_micro=max_cost_micro,
        seconds=float(seconds), admission_deadline=admission_deadline,
    )
    _validate_prepared(job)
    return job


def _validate_prepared(job: PreparedMediaJob, *, quoted: bool = False) -> None:
    if not isinstance(job, PreparedMediaJob):
        raise MediaJobError("prepared video job required")
    if job.admission_deadline is not None and (
        not isinstance(job.admission_deadline, datetime) or job.admission_deadline.tzinfo is None
    ):
        raise MediaJobError("timezone-aware async admission deadline required")
    _uuid(job.job_id)
    _uuid(job.account_id)
    for digest in (job.idempotency_hash, job.request_hash, job.payload_hash):
        _digest(digest)
    if ((not quoted and job.job_id != str(uuid5(_NAMESPACE, f"{job.account_id}:{job.idempotency_hash}")))
            or (quoted and _uuid(job.job_id).version != 4)):
        raise MediaJobError("job id does not match request identity")
    if (type(job.max_cost_micro) is not int or not 0 < job.max_cost_micro <= 2**63 - 1
            or not isinstance(job.model, str) or not 0 < len(job.model) <= 255
            or not isinstance(job.preferred_worker, str) or len(job.preferred_worker) > 255):
        raise MediaJobError("invalid prepared video limits")
    if (not isinstance(job.payload_json, str) or len(job.payload_json) > MAX_PAYLOAD_BYTES
            or not isinstance(job.route_json, str) or len(job.route_json) > MAX_PAYLOAD_BYTES):
        raise MediaJobError("journal JSON exceeds size limit")
    payload, models = job.payload, job.route_models
    if (not isinstance(payload, dict) or type(payload.get(MARKER)) is not int or payload[MARKER] != 1
            or type(payload.get("n")) is not int or payload["n"] != 1 or payload.get("recipe_engine") != "comfyui"
            or not isinstance(payload.get("recipe_spec"), dict) or not payload["recipe_spec"]
            or not isinstance(models, list) or not 1 <= len(models) <= 8
            or not all(isinstance(model, str) and 0 < len(model) <= 255 for model in models)
            or len(set(models)) != len(models)):
        raise MediaJobError("compiled single-video recipe required")
    root = payload.get("recipe_root")
    if not isinstance(root, str) or not re.fullmatch(r"0x[0-9a-f]{64}", root):
        raise MediaJobError("governed recipe commitment required")
    frames, fps = payload.get("frames"), payload.get("fps")
    if (type(frames) is not int or not 9 <= frames <= 337 or frames % 8 != 1
            or type(fps) is not int or fps != 24
            or type(job.seconds) not in (int, float) or not math.isfinite(job.seconds)
            or job.seconds != frames / fps or payload.get("seconds") != job.seconds):
        raise MediaJobError("compiled video timing does not match billing units")
    if (job.payload_json != _json(payload) or job.route_json != _json(models)
            or job.payload_hash != _payload_hash(payload, models, job.model, job.preferred_worker)):
        raise MediaJobError("prepared video commitment mismatch")


def _require_admission_deadline(job: PreparedMediaJob) -> None:
    if job.admission_deadline is not None and job.admission_deadline <= _now():
        raise MediaJobAdmissionClosed("async admission deadline expired")


async def insert_in_session(session, job: PreparedMediaJob, *, prepaid: bool = False, source_objects=None) -> None:
    """Called only inside the credit or proved-video hold/admission transaction."""
    _validate_prepared(job, quoted=prepaid)
    _require_admission_deadline(job)
    hold = (await session.execute(sa.select(reservations).where(
        reservations.c.job_id == job.job_id))).mappings().one_or_none()
    if (hold is None or hold["account_id"] != job.account_id or hold["model"] != job.model
            or hold["status"] != "held" or hold["billing_source"] != ("video_prepaid" if prepaid else "credits")
            or not 0 < hold["reserved_micro"] <= job.max_cost_micro):
        raise MediaJobError("async video requires its matching positive hold")
    if prepaid:
        from . import video_prepaid
        await video_prepaid.require_prepared_in_session(session, job, source_objects)
    else:
        source_objects = media_source_retention.capture(job.payload)
    await session.execute(media_jobs.insert().values(
        job_id=job.job_id, account_id=job.account_id, idempotency_hash=job.idempotency_hash,
        request_hash=job.request_hash, payload_hash=job.payload_hash, model=job.model,
        payload=job.payload, route_models=job.route_models, preferred_worker=job.preferred_worker,
        max_cost_micro=job.max_cost_micro, source_objects=source_objects, created=_now(),
    ))
    _require_admission_deadline(job)


async def _lookup(session, account_id, key_hash: str, request_hash: str) -> dict | None:
    family = await identities.account_family_ids(account_id, session=session, max_members=128)
    rows = (await session.execute(sa.select(media_jobs).where(
        media_jobs.c.account_id.in_(family), media_jobs.c.idempotency_hash == key_hash,
    ).limit(2))).mappings().all()
    if len(rows) > 1:
        raise MediaJobConflict("idempotency key is ambiguous after account merge")
    if not rows:
        return None
    if rows[0]["request_hash"] != request_hash:
        raise MediaJobConflict("idempotency key already belongs to a different request")
    return dict(rows[0])


async def lookup_request(account_id, idempotency_key: str, request_hash: str) -> dict | None:
    """Authenticate externally, then replay before compiling/uploading again."""
    key_hash, request_hash = _key_hash(idempotency_key), _digest(request_hash)
    async with await new_session() as session:
        return await _lookup(session, account_id, key_hash, request_hash)


async def reserve(job: PreparedMediaJob, *, user: dict | None = None, concurrency_limit: int | None = None) -> dict:
    """Serialize request identity across pocket draws; hold+journal commit together.

    PostgreSQL is the concurrency authority. NO KEY UPDATE blocks identity merges
    while permitting FK checks in the existing separately committed pocket helpers.
    SQLite supports sequential compatibility tests, not concurrent guarantees.
    """
    from . import credits

    _validate_prepared(job)
    if concurrency_limit is not None and (type(concurrency_limit) is not int or not 1 <= concurrency_limit <= 16):
        raise MediaJobError("invalid async video concurrency limit")
    if user is not None and _uuid(user.get("account_id")) != job.account_id:
        raise MediaJobError("billing principal does not own prepared video job")
    async with await new_session() as session:
        canonical = await identities.canonical_account_id(job.account_id, session=session)
        locked = (await session.execute(sa.select(accounts.c.id).where(
            accounts.c.id.in_({job.account_id, canonical}),
        ).order_by(accounts.c.id).with_for_update(key_share=True))).scalars().all()
        if canonical not in locked:
            raise MediaJobError("account is unavailable")
        if await identities.canonical_account_id(job.account_id, session=session) != canonical:
            raise MediaJobConflict("account changed during video admission")
        existing = await _lookup(session, canonical, job.idempotency_hash, job.request_hash)
        if existing is not None:
            return existing
        _require_admission_deadline(job)
        if canonical != job.account_id:
            raise MediaJobConflict("new video requests require the current canonical account")
        if concurrency_limit is not None:
            family = await identities.account_family_ids(canonical, session=session, max_members=128)
            held = await session.scalar(sa.select(sa.func.count()).select_from(media_jobs.join(
                reservations, media_jobs.c.job_id == reservations.c.job_id,
            )).where(media_jobs.c.account_id.in_(family), reservations.c.status == "held"))
            if held >= concurrency_limit:
                raise MediaJobCapacityExceeded("too many held async video jobs")
        authorization = await credits.authorize_media(
            job.account_id, job.model, "video", 1, job.seconds, job.job_id,
            record_reservation=True, user=user, max_cost_micro_usd=job.max_cost_micro,
            prepared_media_job=job,
        )
        if not authorization["ok"]:
            raise MediaJobPaymentRequired(authorization["status"])
        stored = await _lookup(session, canonical, job.idempotency_hash, job.request_hash)
        if stored is None:
            raise MediaJobError("credit hold has no matching durable video request")
        return stored


async def recover(account_id, job_id=None, *, idempotency_key: str | None = None) -> dict | None:
    """Owner-only status; uncertain execution is explicit, not an automatic retry."""
    if (job_id is None) == (idempotency_key is None):
        raise MediaJobError("exactly one video recovery selector required")
    selector = (media_jobs.c.job_id == str(_uuid(job_id)) if job_id is not None
                else media_jobs.c.idempotency_hash == _key_hash(idempotency_key))
    async with await new_session() as session:
        family = await identities.account_family_ids(account_id, session=session, max_members=128)
        rows = (await session.execute(sa.select(
            media_jobs.c.job_id, media_jobs.c.model, media_jobs.c.max_cost_micro, media_jobs.c.request_hash,
            media_jobs.c.dispatch_state, media_jobs.c.execution_started,
            reservations.c.status, reservations.c.reserved_micro, reservations.c.actual_micro,
            reservations.c.media_result,
        ).join(reservations, media_jobs.c.job_id == reservations.c.job_id).where(
            selector, media_jobs.c.account_id.in_(family),
        ).limit(2))).mappings().all()
    if not rows:
        return None
    if len(rows) != 1:
        raise MediaJobConflict("video request is ambiguous after account merge")
    row = rows[0]
    result = row["media_result"]
    if result is not None:
        if row["status"] != "settled":
            raise MediaJobError("uncommitted video result")
        result = media_results.validate_result(result)
    state = ("completed" if result is not None else "closed_without_result" if row["status"] != "held"
             else "execution_uncertain" if row["execution_started"] is not None
             else "queued" if row["dispatch_state"] == "queued" else "pending_dispatch")
    return {"job_id": row["job_id"], "model": row["model"], "state": state,
            "request_hash": row["request_hash"],
            "reserved_micro": row["reserved_micro"], "max_cost_micro": row["max_cost_micro"],
            "actual_micro": row["actual_micro"], "result": result}


async def _locked_job(session, job_id) -> dict | None:
    # Same job lock as release/settlement: a refund cannot race a new execution claim.
    await validator_audit_budgets.lock_job_in_session(session, job_id)
    row = (await session.execute(sa.select(media_jobs, reservations.c.status,
                                          reservations.c.reserved_micro, reservations.c.billing_source,
                                          reservations.c.account_id.label("hold_owner"),
                                          reservations.c.model.label("hold_model"))
        .join(reservations, media_jobs.c.job_id == reservations.c.job_id)
        .where(media_jobs.c.job_id == str(job_id)).with_for_update())).mappings().one_or_none()
    return dict(row) if row else None


async def _billing_authorized(session, row):
    if row["billing_source"] == "credits":
        return True
    if row["billing_source"] == "video_prepaid":
        from . import video_prepaid
        await video_prepaid.require_hold_in_session(session, row["job_id"])
        return True
    return False


async def dispatch_one(job_id, *, lease_seconds: int = 60) -> bool:
    """One bounded outbox attempt; failed/lost XADD replies leave a reclaimable lease.

    Duplicate Redis entries are permitted. The SQL execution fence, not XADD or
    its reply, guarantees at-most-one worker dispatch. The background batch can
    reclaim unstarted queued jobs after lease expiry, including after Redis loss.
    """
    job_id = str(_uuid(job_id))
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise MediaJobError("invalid dispatch lease")
    async with await new_session() as session:
        row = await _locked_job(session, job_id)
        now = _now()
        if (row is None or row["status"] != "held" or row["execution_started"] is not None
                or row["dispatch_attempts"] >= MAX_DISPATCH_ATTEMPTS
                or (row["dispatch_deadline"] is not None and _aware(row["dispatch_deadline"]) > now)):
            return False
        if not await _billing_authorized(session, row):
            raise MediaJobError("async video funding is unavailable")
        token = str(uuid4())
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == job_id).values(
            dispatch_state="dispatching", dispatch_token=token,
            dispatch_deadline=now + timedelta(seconds=lease_seconds),
            dispatch_attempts=media_jobs.c.dispatch_attempts + 1,
        ))
        await session.commit()
    stream_id = await asyncio.wait_for(job_queue.submit_job(
        job_id, row["payload"], row["route_models"], job_type="video",
        preferred_worker=row["preferred_worker"],
    ), timeout=min(5, lease_seconds / 2))
    if not isinstance(stream_id, str) or not _QUEUE_ID.fullmatch(stream_id):
        raise MediaJobError("invalid queue submission acknowledgement")
    async with await new_session() as session:
        # A worker may have claimed/executed while the publisher awaited XADD.
        await session.execute(media_jobs.update().where(
            media_jobs.c.job_id == job_id, media_jobs.c.dispatch_token == token,
        ).values(dispatch_state="queued", queue_message_id=stream_id))
        await session.commit()
    return True


async def dispatch_batch(*, limit: int = 10, lease_seconds: int = 60) -> dict:
    """Drain held requests even after admission rollback; never rerender a claim.

    Re-publish unclaimed queued work after its bounded lease. This covers Redis
    loss/trimming without treating transport state as execution authority.
    Exhaustion releases only unstarted jobs under the existing financial lock.
    """
    from . import credits

    if type(limit) is not int or not 1 <= limit <= 50:
        raise MediaJobError("invalid async dispatch batch limit")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise MediaJobError("invalid dispatch lease")
    async with await new_session() as session:
        rows = (await session.execute(sa.select(media_jobs.c.job_id, media_jobs.c.dispatch_attempts)
            .join(reservations, media_jobs.c.job_id == reservations.c.job_id)
            .where(reservations.c.status == "held", media_jobs.c.execution_started.is_(None),
                   sa.or_(media_jobs.c.dispatch_deadline.is_(None), media_jobs.c.dispatch_deadline <= _now()))
            .order_by(media_jobs.c.created, media_jobs.c.job_id).limit(limit))).all()
    counts = {"queued": 0, "exhausted": 0, "errors": 0}
    for job_id, attempts in rows:
        try:
            if attempts >= MAX_DISPATCH_ATTEMPTS:
                await credits.release_job(job_id, only_if_unstarted_media=True)
                counts["exhausted"] += 1
            elif await dispatch_one(job_id, lease_seconds=lease_seconds):
                counts["queued"] += 1
        except Exception:
            # The retained lease is recovery authority. Do not expose DB/queue
            # exceptions, refund uncertain acceptance or skip the remaining batch.
            counts["errors"] += 1
    return counts


async def claim_execution(*, job_id, payload: dict, route_models: list[str],
                          preferred_worker: str, selected_model: str, worker_id, job_type: str,
                          execution_token: str) -> str:
    """Commit before sending any WS job; DB failure or ambiguity must not dispatch."""
    job_id, worker_id = str(_uuid(job_id)), _uuid(worker_id)
    execution_token = _execution_token(execution_token)
    async with await new_session() as session:
        row = await _locked_job(session, job_id)
        if (row is None or job_type != "video" or row["payload_hash"] != _payload_hash(
                payload, route_models, row["model"], preferred_worker)
                or type(payload.get(MARKER)) is not int or payload.get(MARKER) != 1
                or selected_model not in row["route_models"]
                or row["hold_owner"] != row["account_id"] or row["hold_model"] != row["model"]
                or not await _billing_authorized(session, row)
                or not 0 < row["reserved_micro"] <= row["max_cost_micro"]):
            raise MediaJobError("queue job does not match durable video authorization")
        if row["status"] != "held":
            return "closed"
        if row["execution_started"] is not None:
            return "duplicate"
        now = _now()
        updated = await session.execute(media_jobs.update().where(
            media_jobs.c.job_id == job_id, media_jobs.c.execution_started.is_(None),
        ).values(execution_worker_id=worker_id, execution_started=now, execution_model=selected_model,
                 execution_token=execution_token, execution_deadline=now + timedelta(seconds=EXECUTION_LEASE_SECONDS)))
        if updated.rowcount != 1:
            raise MediaJobError("execution claim did not commit one job")
        await session.commit()
        return "claimed"


def _execution_token(value: str) -> str:
    token = _uuid(value)
    if token.version != 4 or str(token) != value:
        raise MediaJobError("canonical execution lease token required")
    return value


def _live_execution(row: dict, now: datetime) -> bool:
    return (
        row["status"] == "held"
        and row["execution_deadline"] is not None
        and _aware(row["execution_deadline"]) > now
        and row["execution_started"] is not None
        and _aware(row["execution_started"]) + timedelta(seconds=MAX_EXECUTION_SECONDS) > now
    )


async def terminal_authorized_in_session(
    session: AsyncSession, *, job_id: str, execution_token: str | None, worker_id: UUID | str | None = None, model: str | None = None,
) -> bool:
    """Caller owns the financial job lock. An old WS cannot charge or refund.

    Unmarked legacy jobs retain their existing terminal path. Journal jobs always
    require their current Core lease; NULL pre-0044 rows are never reconstructed.
    Closed rows may repeat the matching terminal, but cannot move money again.
    """
    row = (
        (
            await session.execute(
                sa.select(media_jobs, reservations.c.status)
                .join(
                    reservations,
                    media_jobs.c.job_id == reservations.c.job_id,
                )
                .where(media_jobs.c.job_id == str(job_id))
                .with_for_update(),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return execution_token is None
    if (
        execution_token is None
        or row["execution_token"] != _execution_token(execution_token)
        or (worker_id is not None and row["execution_worker_id"] != _uuid(worker_id))
        or (model is not None and row["execution_model"] != model)
    ):
        return False
    return row["status"] != "held" or _live_execution(dict(row), _now())


async def renew_execution(*, job_id: str, worker_id: UUID | str, execution_token: str) -> bool:
    """A lease expires irrevocably for this session, including during DB failure."""
    job_id, worker_id, token = str(_uuid(job_id)), _uuid(worker_id), _execution_token(execution_token)
    async with await new_session() as session:
        row = await _locked_job(session, job_id)
        now = _now()
        if row is None or row["execution_worker_id"] != worker_id or row["execution_token"] != token or not _live_execution(row, now):
            return False
        deadline = min(
            now + timedelta(seconds=EXECUTION_LEASE_SECONDS), _aware(row["execution_started"]) + timedelta(seconds=MAX_EXECUTION_SECONDS),
        )
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == job_id).values(execution_deadline=deadline))
        await session.commit()
        return True


async def detach_execution(*, job_id: str, execution_token: str) -> None:
    """End only this session's lease; never clear the original execution fence."""
    async with await new_session() as session:
        row = await _locked_job(session, str(_uuid(job_id)))
        if row is not None and row["execution_token"] == _execution_token(execution_token):
            await session.execute(media_jobs.update().where(media_jobs.c.job_id == row["job_id"]).values(execution_deadline=_now()))
            await session.commit()


async def claim_resumption(*, worker_id: UUID | str, models: list[str], media_features: list[str]) -> dict[str, Any] | None:
    """Authenticated original worker only, outside Redis routing and new admission.

    Return the immutable execution and a private Core lease. The WS envelope MUST
    carry resume:true: a worker missing its journal must refuse, not render again.
    A live lease, mismatched capability or legacy NULL lease cannot be reclaimed.
    """
    if "async-video-resume-v1" not in media_features:
        return None
    worker_id = _uuid(worker_id)
    now = _now()
    async with await new_session() as session:
        candidates = (
            (
                await session.execute(
                    sa.select(media_jobs.c.job_id)
                    .join(
                        reservations,
                        media_jobs.c.job_id == reservations.c.job_id,
                    )
                    .where(
                        media_jobs.c.execution_worker_id == worker_id,
                        reservations.c.status == "held",
                        media_jobs.c.execution_deadline <= now,
                        media_jobs.c.execution_started > now - timedelta(seconds=MAX_EXECUTION_SECONDS),
                    )
                    .order_by(media_jobs.c.created, media_jobs.c.job_id)
                    .limit(16),
                )
            )
            .scalars()
            .all()
        )
    for job_id in candidates:
        async with await new_session() as session:
            row = await _locked_job(session, job_id)
            now = _now()
            if (
                row is None
                or row["status"] != "held"
                or row["execution_worker_id"] != worker_id
                or row["execution_token"] is None
                or row["execution_deadline"] is None
                or _aware(row["execution_deadline"]) > now
                or row["execution_started"] is None
                or _aware(row["execution_started"]) + timedelta(seconds=MAX_EXECUTION_SECONDS) <= now
            ):
                continue
            payload = row["payload"]
            required = payload.get("required_media_features", [])
            feature = payload.get("required_media_feature")
            if (
                row["execution_model"] not in models
                or not isinstance(required, list)
                or len(required) > 8
                or not all(isinstance(item, str) and item in media_features for item in required)
                or (feature and feature not in media_features)
            ):
                continue
            if (
                row["hold_owner"] != row["account_id"]
                or row["hold_model"] != row["model"]
                or not await _billing_authorized(session, row)
                or not 0 < row["reserved_micro"] <= row["max_cost_micro"]
                or row["execution_model"] not in row["route_models"]
                or row["payload_hash"] != _payload_hash(payload, row["route_models"], row["model"], row["preferred_worker"])
            ):
                raise MediaJobError("resumption does not match original video authorization")
            token = str(uuid4())
            await session.execute(
                media_jobs.update()
                .where(media_jobs.c.job_id == job_id)
                .values(
                    execution_token=token,
                    execution_deadline=min(
                        now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
                        _aware(row["execution_started"]) + timedelta(seconds=MAX_EXECUTION_SECONDS),
                    ),
                ),
            )
            await session.commit()
            return {
                "job_id": job_id,
                "job_type": "video",
                "payload": payload,
                "models": row["route_models"],
                "preferred_worker": row["preferred_worker"],
                "selected_model": row["execution_model"],
                "execution_token": token,
                "worker_id": str(worker_id),
            }
    return None
