# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Default-off pairing; no public metadata or cross-account credential transfer."""

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from ..auth import extract_api_key
from ..config import get_settings
from ..ratelimit import limiter
from ..services import accounts, user_tokens
from ..services import validator_pairing as pairing

router = APIRouter()


class ConfirmForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: SecretStr = Field(min_length=130, max_length=132)


class UnlinkForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairing_id: str = Field(pattern=r"^vpa_[0-9a-f]{64}$")


class NodeUnlinkForm(UnlinkForm):
    issued_at: int = Field(strict=True, ge=1)
    signature: SecretStr = Field(min_length=130, max_length=132)


def _enabled(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not get_settings().validator_pairing_enabled:
        raise HTTPException(503, detail="Validator account pairing is not enabled")


async def _node_user(apikey, authorization):
    user = await accounts.authenticate(extract_api_key(apikey, authorization), required_scope="validator.attest")
    if user.get("source") != "v2" or not user.get("account_id"):
        raise HTTPException(403, detail="A registered validator account is required")
    return user


async def _account_user(apikey, authorization, *, fresh=True):
    user = await accounts.authenticate(extract_api_key(apikey, authorization), required_scope="account.manage" if fresh else "account.read")
    if user.get("key_kind") != "user_token":
        raise HTTPException(403, detail="Sign in to your account with Google or a wallet")
    if fresh:
        user_tokens.require_recent_step_up(user.get("token_claims") or {})
    return user


async def _call(action, **kwargs):
    try:
        return await action(**kwargs)
    except pairing.PairingError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    except SQLAlchemyError:
        # Driver errors may contain account IDs and signed payload parameters.
        # Preserve the transaction's rollback and return a retryable safe error.
        raise HTTPException(503, detail="Pairing storage is unavailable; retry without creating a new node") from None


@router.post("/v1/validator/account-pairings")
@limiter.limit("10/hour")
async def create_pairing(request: Request, response: Response, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(pairing.create, account_id=user["account_id"], wallet=user.get("wallet"))


@router.get("/v1/validator/account-pairing")
@limiter.limit("30/minute")
async def poll_pairing(request: Request, response: Response, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(pairing.poll, account_id=user["account_id"], wallet=user.get("wallet"))


@router.post("/v1/validator/account-pairings/{pairing_id}/confirm")
@limiter.limit("10/minute")
async def confirm_pairing(
    request: Request,
    response: Response,
    pairing_id: str,
    form: ConfirmForm,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(
        pairing.confirm,
        pairing_id=pairing_id,
        account_id=user["account_id"],
        wallet=user.get("wallet"),
        signature=form.signature.get_secret_value(),
    )


@router.post("/v1/validator/account-pairings/{pairing_id}/cancel")
@limiter.limit("10/minute")
async def cancel_pairing(
    request: Request, response: Response, pairing_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(pairing.cancel, pairing_id=pairing_id, account_id=user["account_id"], wallet=user.get("wallet"))


@router.get("/v1/validator/account-link")
@limiter.limit("30/minute")
async def node_link(request: Request, response: Response, apikey: str | None = Header(None), authorization: str | None = Header(None)):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(pairing.node_link, account_id=user["account_id"], wallet=user.get("wallet"))


@router.post("/v1/validator/account-link/unlink")
@limiter.limit("10/minute")
async def unlink_from_node(
    request: Request, response: Response, form: NodeUnlinkForm, apikey: str | None = Header(None), authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _node_user(apikey, authorization)
    return await _call(
        pairing.unlink_from_node,
        account_id=user["account_id"],
        wallet=user.get("wallet"),
        pairing_id=form.pairing_id,
        issued_at=form.issued_at,
        signature=form.signature.get_secret_value(),
    )


@router.get("/v1/account/validator-pairings/{pairing_id}")
@limiter.limit("30/minute")
async def inspect_pairing(
    request: Request, response: Response, pairing_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _account_user(apikey, authorization)
    return await _call(pairing.inspect, pairing_id=pairing_id, operator_account_id=user["account_id"])


@router.post("/v1/account/validator-pairings/{pairing_id}/approve")
@limiter.limit("10/minute")
async def approve_pairing(
    request: Request, response: Response, pairing_id: str, apikey: str | None = Header(None), authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _account_user(apikey, authorization)
    return await _call(pairing.approve, pairing_id=pairing_id, operator_account_id=user["account_id"])


@router.get("/v1/account/validators")
@limiter.limit("30/minute")
async def account_validators(
    request: Request, response: Response, apikey: str | None = Header(None), authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _account_user(apikey, authorization, fresh=False)
    return await _call(pairing.list_for_account, operator_account_id=user["account_id"])


@router.post("/v1/account/validators/{validator_id}/unlink")
@limiter.limit("10/minute")
async def unlink_validator(
    request: Request,
    response: Response,
    validator_id: str,
    form: UnlinkForm,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    _enabled(response)
    user = await _account_user(apikey, authorization)
    return await _call(pairing.unlink, validator_id=validator_id, operator_account_id=user["account_id"], pairing_id=form.pairing_id)
