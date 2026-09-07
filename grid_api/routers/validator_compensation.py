# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Default-off private compensation status and signature collection; never pay."""

import json

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts, user_tokens
from ..services import validator_compensation as comp
from ..services import validator_compensation_operator as service
from ..services import validator_pairing as pairing

PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private(request):
            try:
                response = await handler(request)
                response.headers.update(PRIVATE)
                return response
            except StarletteHTTPException as exc:
                exc.headers = {**(exc.headers or {}), **PRIVATE}
                raise
            except RequestValidationError:
                raise HTTPException(400, detail="Invalid compensation request", headers=PRIVATE) from None

        return private


router = APIRouter(route_class=PrivateRoute)


class PrepareForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(pattern=r"^0x[0-9a-f]{40}$")


class SignForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: SecretStr = Field(min_length=4, max_length=16386)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


async def _form(request, model):
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, detail="Encoded request bodies are not accepted")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 20_000:
            raise HTTPException(413, detail="Compensation request is too large")
        data.extend(chunk)
    try:
        return model.model_validate(json.loads(data, object_pairs_hook=_unique))
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise HTTPException(400, detail="Invalid compensation request") from None


async def _auth(apikey, authorization, *, human=False, write=False):
    try:
        service._enabled()
    except service.OperatorError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    scope = ("account.manage" if write else "account.read") if human else ("validator.attest" if write else "validator.read")
    user = await accounts.authenticate(extract_api_key(apikey, authorization), required_scope=scope)
    if user.get("source") != "v2" or not user.get("account_id"):
        raise HTTPException(403, detail="A canonical account is required")
    if human:
        if user.get("key_kind") != "user_token":
            raise HTTPException(403, detail="Sign in with Google or a wallet")
        if write:
            user_tokens.require_recent_step_up(user.get("token_claims") or {})
    return user


async def _call(action, **kwargs):
    try:
        return await action(**kwargs)
    except service.OperatorError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    except pairing.PairingError:
        raise HTTPException(409, detail="account_link_unavailable") from None
    except comp.CompensationError:
        raise HTTPException(409, detail="allocation_requires_review") from None
    except SQLAlchemyError:
        raise HTTPException(503, detail="compensation_storage_unavailable") from None


@router.get("/v1/validator/compensation")
@limiter.limit("30/minute")
async def node_status(
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    user = await _auth(apikey, authorization)
    return await _call(service.status, account_id=user["account_id"], wallet=user.get("wallet"), offset=offset)


@router.post("/v1/validator/compensation/{allocation_hash}/requests")
@limiter.limit("10/hour")
async def start_request(
    request: Request,
    allocation_hash: str,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    user = await _auth(apikey, authorization, write=True)
    return await _call(service.start, account_id=user["account_id"], wallet=user.get("wallet"), allocation_hash=allocation_hash)


@router.get("/v1/validator/compensation/requests/{request_id}")
@limiter.limit("30/minute")
async def node_request(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization)
    return await _call(service.inspect, account_id=user["account_id"], wallet=user.get("wallet"), request_id=request_id)


@router.post("/v1/validator/compensation/requests/{request_id}/confirm")
@limiter.limit("10/minute")
async def node_confirm(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization, write=True)
    form = await _form(request, SignForm)
    return await _call(
        service.approve,
        account_id=user["account_id"],
        wallet=user.get("wallet"),
        request_id=request_id,
        review_hash=form.review_hash,
        signature=form.signature.get_secret_value(),
    )


@router.post("/v1/validator/compensation/requests/{request_id}/cancel")
@limiter.limit("10/minute")
async def node_cancel(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization, write=True)
    return await _call(service.cancel, account_id=user["account_id"], wallet=user.get("wallet"), request_id=request_id)


@router.get("/v1/account/validator-compensation/requests/{request_id}")
@limiter.limit("30/minute")
async def account_request(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization, human=True)
    return await _call(service.inspect, account_id=user["account_id"], wallet=None, request_id=request_id, human=True)


@router.post("/v1/account/validator-compensation/requests/{request_id}/prepare")
@limiter.limit("10/minute")
async def account_prepare(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization, human=True, write=True)
    form = await _form(request, PrepareForm)
    return await _call(service.prepare, account_id=user["account_id"], request_id=request_id, recipient=form.recipient)


@router.post("/v1/account/validator-compensation/requests/{request_id}/approve")
@limiter.limit("10/minute")
async def account_approve(request: Request, request_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    user = await _auth(apikey, authorization, human=True, write=True)
    form = await _form(request, SignForm)
    return await _call(
        service.approve,
        account_id=user["account_id"],
        wallet=None,
        request_id=request_id,
        review_hash=form.review_hash,
        signature=form.signature.get_secret_value(),
        human=True,
    )
