# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rig-scoped, read-only authority for private video-cache retention."""

import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import new_session
from ..ratelimit import limiter
from ..services import identities
from ..services.media_jobs import MAX_EXECUTION_SECONDS
from ..v2.schema import media_jobs, reservations, workers
from .accounts import _require_worker_self

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 2048
MAX_JOB_IDS = 32
SCHEMA = "aipg.worker.media-journal.v1"
PRIVATE = {"Cache-Control": "no-store"}


class BoundedWorkerMediaRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise HTTPException(415, detail="Content-Type must be application/json")
                length = request.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdigit() or len(length) > 20:
                        raise HTTPException(400, detail="Invalid Content-Length")
                    if int(length) > MAX_BODY_BYTES:
                        raise HTTPException(413, detail="Worker status request exceeds its body limit")
                content = bytearray()
                async for chunk in request.stream():
                    if len(content) + len(chunk) > MAX_BODY_BYTES:
                        raise HTTPException(413, detail="Worker status request exceeds its body limit")
                    content.extend(chunk)
                request._body = bytes(content)
                response = await original(request)
                response.headers.update(PRIVATE)
                return response
            except (RequestValidationError, RecursionError):
                return JSONResponse({"detail": "Invalid worker status request"}, status_code=422, headers=PRIVATE)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={**(exc.headers or {}), **PRIVATE})

        return guarded


router = APIRouter(route_class=BoundedWorkerMediaRoute)


class WorkerMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_ids: list[str] = Field(min_length=1, max_length=MAX_JOB_IDS)

    @field_validator("job_ids")
    @classmethod
    def canonical_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(str(UUID(value)) != value for value in values):
            raise ValueError("Unique canonical job UUIDs required")
        return values


class WorkerMediaState(BaseModel):
    job_id: str
    state: Literal["held", "closed", "unknown"]


class WorkerMediaResponse(BaseModel):
    schema_name: Literal["aipg.worker.media-journal.v1"] = Field(alias="schema")
    worker_id: str
    jobs: list[WorkerMediaState]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.post("/v1/workers/self/media-jobs", response_model=WorkerMediaResponse)
@limiter.limit("120/minute")
async def worker_media_status(
    request: Request,
    body: WorkerMediaRequest,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
) -> JSONResponse:
    try:
        user, name = await _require_worker_self(apikey, authorization)
        now = datetime.now(UTC)
        async with await new_session() as session:
            family = await identities.account_family_ids(user["account_id"], session=session)
            worker_id = (
                await session.execute(
                    sa.select(workers.c.id).where(
                        workers.c.account_id.in_(family),
                        workers.c.name == name,
                    ),
                )
            ).scalar_one_or_none()
            if worker_id is None:
                raise HTTPException(404, detail="Registered worker not found")
            # media_jobs.account_id is the paying customer, NOT the worker owner.
            rows = (
                (
                    await session.execute(
                        sa.select(
                            media_jobs.c.job_id,
                            reservations.c.status,
                            media_jobs.c.execution_started,
                            media_jobs.c.execution_deadline,
                            media_jobs.c.execution_token,
                        )
                        .join(reservations, reservations.c.job_id == media_jobs.c.job_id)
                        .where(
                            media_jobs.c.execution_worker_id == worker_id,
                            media_jobs.c.job_id.in_(body.job_ids),
                        ),
                    )
                )
                .mappings()
                .all()
            )
        states: dict[str, str] = {}
        for row in rows:
            state = "unknown"
            if row["status"] == "settled":
                state = "closed"
            elif (
                row["status"] == "held"
                and row["execution_started"] is not None
                and row["execution_deadline"] is not None
                and row["execution_token"] is not None
                and _aware(row["execution_started"]) <= now
                and _aware(row["execution_started"]) + timedelta(seconds=MAX_EXECUTION_SECONDS) > now
                and _aware(row["execution_deadline"]) > now
            ):
                state = "held"
            states[row["job_id"]] = state
        return JSONResponse(
            {
                "schema": SCHEMA,
                "worker_id": str(worker_id),
                "jobs": [{"job_id": job_id, "state": states.get(job_id, "unknown")} for job_id in body.job_ids],
            },
            headers=PRIVATE,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Worker media status unavailable error_type=%s", type(exc).__name__)
        return JSONResponse({"detail": "Worker media status temporarily unavailable"}, status_code=503, headers=PRIVATE)
