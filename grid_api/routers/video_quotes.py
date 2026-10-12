# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Default-off, allowlisted quotation only; there is no payment endpoint."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import extract_api_key
from ..config import get_settings
from ..ratelimit import limiter
from ..services import accounts, video_plans
from ..services import video_quotes as quotes
from .video_jobs import AsyncVideoRequest, BoundedVideoRoute, request_hash
from .videos import VideoRequest, validate_video

router = APIRouter(route_class=BoundedVideoRoute)
PRIVATE = {"Cache-Control": "no-store"}
PATH = "/v1/x402/videos/quotes"


async def _authenticate(apikey, authorization, assertion, token):
    return await accounts.authenticate(extract_api_key(apikey, authorization), assertion,
                                       user_token=token, required_scope="inference.submit")


def _error(exc: Exception) -> JSONResponse:
    headers = dict(PRIVATE)
    if isinstance(exc, HTTPException):
        code, detail = exc.status_code, exc.detail
        headers.update(exc.headers or {})
    elif isinstance(exc, quotes.VideoQuoteConflict):
        code, detail = 409, "Idempotency key conflicts with another video quote"
    elif isinstance(exc, quotes.VideoQuoteCapacity):
        code, detail = 429, "Private video quote capacity is exhausted"
    elif isinstance(exc, quotes.VideoQuoteUnpriced):
        code, detail = 422, "A positive governed video price within the cost ceiling is required"
    else:
        code, detail = 503, "Video quote unavailable; recover with the same idempotency key"
    return JSONResponse({"detail": detail}, status_code=code, headers=headers)


def _key(key) -> None:
    try:
        quotes.validate_idempotency_key(key)
    except quotes.VideoQuoteError:
        raise HTTPException(422, detail="A UUIDv4 Idempotency-Key is required") from None


def _admit(settings, user) -> None:
    if (not settings.video_x402_quotes_enabled
            or UUID(str(user["account_id"])) not in settings.video_x402_quote_accounts
            or settings.video_x402_quotes_until is None
            or datetime.now(UTC) >= settings.video_x402_quotes_until):
        raise HTTPException(503, detail="Private video quotation is disabled")


@router.post(PATH)
@limiter.limit("4/minute")
async def create_video_quote(
    request: Request, body: AsyncVideoRequest,
    idempotency_key: str | None = Header(None, max_length=64),
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    try:
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        _key(idempotency_key)
        settings = get_settings()
        row = await quotes.lookup(user["account_id"], idempotency_key=idempotency_key)
        limits = {"per_account_limit": settings.video_x402_quote_per_account,
                  "global_limit": settings.video_x402_quote_capacity}
        if row is None:
            _admit(settings, user)
            await quotes.check_capacity(user["account_id"], **limits)
        with quotes.preparation_slot(user["account_id"]):
            intent = request_hash(body)
            if row is not None:
                if row["request_hash"] != intent:
                    raise quotes.VideoQuoteConflict("quote intent changed")
                created = False
            else:
                native = body.model_dump(exclude_unset=True, exclude_none=True, exclude={"max_cost_micro_usd"})
                native["model"] = body.model
                inputs = await validate_video(VideoRequest.model_validate(native), user, async_submission=True)
                plan = video_plans.compile_video(inputs)
                prepared = quotes.prepare(
                    account_id=user["account_id"], idempotency_key=idempotency_key, request_hash=intent, plan=plan,
                    max_cost_micro=body.max_cost_micro_usd, network=settings.video_x402_quote_network,
                    pay_to=settings.video_x402_quote_pay_to, server_max_micro=settings.video_x402_quote_max_micro,
                    preferred_worker=body.worker or "",
                )
                _admit(settings, user)  # Recheck pilot expiry after bounded source work.
                row, created = await quotes.persist(prepared, idempotency_key=idempotency_key,
                                                   admission_until=settings.video_x402_quotes_until, **limits)
        return JSONResponse(quotes.view(row), status_code=201 if created else 200,
                            headers={**PRIVATE, "Location": f"{PATH}/{row['id']}"})
    except Exception as exc:
        return _error(exc)


@router.get(PATH + "/{quote_id}")
@limiter.limit("60/minute")
async def get_video_quote(
    request: Request, quote_id: UUID,
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    try:
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        row = await quotes.lookup(user["account_id"], quote_id=quote_id)
        if row is None:
            raise HTTPException(404, detail="No video quote found")
        return JSONResponse(quotes.view(row), headers=PRIVATE)
    except Exception as exc:
        return _error(exc)


@router.get(PATH)
@limiter.limit("60/minute")
async def get_video_quote_request(
    request: Request, idempotency_key: str | None = Header(None, max_length=64),
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    try:
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        _key(idempotency_key)
        row = await quotes.lookup(user["account_id"], idempotency_key=idempotency_key)
        if row is None:
            raise HTTPException(404, detail="No video quote found")
        return JSONResponse(quotes.view(row), headers=PRIVATE)
    except Exception as exc:
        return _error(exc)
