# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dark device-style enrollment for the standalone worker manager."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..auth import extract_api_key
from ..config import get_settings
from ..ratelimit import limiter
from ..services import accounts, user_tokens, worker_enrollment

router = APIRouter()


class CreateEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_signer: str = Field(min_length=42, max_length=42)
    worker_name: str = Field(min_length=1, max_length=120)
    profile_id: str = Field(min_length=1, max_length=128)
    api_key: SecretStr
    poll_token_hash: str = Field(min_length=64, max_length=64)
    valid_days: int = Field(default=90, ge=1, le=365)


class PrepareEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payout_wallet: str = Field(min_length=42, max_length=42)
    replace_payout_wallet: bool = False


class ApproveEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: SecretStr


class PollEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_token: SecretStr


def _enabled() -> None:
    if not get_settings().worker_enrollment_enabled:
        raise HTTPException(503, detail="Native worker enrollment is not enabled.")


async def _recent_account(
    apikey: str | None,
    authorization: str | None,
) -> dict:
    user = await accounts.authenticate(
        extract_api_key(apikey, authorization),
        required_scope="account.manage",
    )
    if user.get("key_kind") not in {"user_token", "delegated_user"}:
        raise HTTPException(403, detail="Worker enrollment needs a fresh Google or wallet proof.")
    user_tokens.require_recent_step_up(user.get("token_claims") or {})
    return user


def _raise_enrollment(exc: worker_enrollment.EnrollmentError) -> None:
    if isinstance(exc, worker_enrollment.EnrollmentUnauthorized):
        raise HTTPException(401, detail=str(exc)) from exc
    if isinstance(exc, worker_enrollment.EnrollmentConflict):
        raise HTTPException(409, detail=str(exc)) from exc
    message = str(exc)
    status = 404 if "not found or has expired" in message else 400
    raise HTTPException(status, detail=message) from exc


@router.post("/v1/workers/enrollments")
@limiter.limit("10/hour")
async def create_worker_enrollment(request: Request, form: CreateEnrollment):
    _enabled()
    try:
        return await worker_enrollment.create_enrollment(
            worker_signer=form.worker_signer,
            worker_name=form.worker_name,
            profile_id=form.profile_id,
            api_key=form.api_key.get_secret_value(),
            poll_token_hash=form.poll_token_hash,
            valid_days=form.valid_days,
        )
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)


@router.get("/v1/workers/enrollments/{enrollment_id}")
@limiter.limit("60/minute")
async def get_worker_enrollment(request: Request, enrollment_id: str):
    _enabled()
    try:
        return await worker_enrollment.public_enrollment(enrollment_id)
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)


@router.post("/v1/workers/enrollments/{enrollment_id}/prepare")
@limiter.limit("20/minute")
async def prepare_worker_enrollment(
    request: Request,
    enrollment_id: str,
    form: PrepareEnrollment,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    _enabled()
    user = await _recent_account(apikey, authorization)
    try:
        return await worker_enrollment.prepare_enrollment(
            enrollment_id,
            user=user,
            payout_wallet=form.payout_wallet,
            replace_payout_wallet=form.replace_payout_wallet,
        )
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)


@router.post("/v1/workers/enrollments/{enrollment_id}/approve")
@limiter.limit("20/minute")
async def approve_worker_enrollment(
    request: Request,
    enrollment_id: str,
    form: ApproveEnrollment,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    _enabled()
    user = await _recent_account(apikey, authorization)
    try:
        return await worker_enrollment.approve_enrollment(
            enrollment_id,
            user=user,
            signature=form.signature.get_secret_value(),
        )
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)


@router.post("/v1/workers/enrollments/{enrollment_id}/poll")
@limiter.limit("60/minute")
async def poll_worker_enrollment(
    request: Request,
    enrollment_id: str,
    form: PollEnrollment,
):
    _enabled()
    try:
        return await worker_enrollment.poll_enrollment(
            enrollment_id,
            poll_token=form.poll_token.get_secret_value(),
        )
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)


@router.post("/v1/workers/enrollments/{enrollment_id}/ack")
@limiter.limit("20/minute")
async def acknowledge_worker_enrollment(
    request: Request,
    enrollment_id: str,
    form: PollEnrollment,
):
    _enabled()
    try:
        return await worker_enrollment.acknowledge_enrollment(
            enrollment_id,
            poll_token=form.poll_token.get_secret_value(),
        )
    except worker_enrollment.EnrollmentError as exc:
        _raise_enrollment(exc)
