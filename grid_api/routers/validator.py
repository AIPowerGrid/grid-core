# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validator endpoints.

These endpoints build the assignment-bound evidence path. They do not route
production jobs, reward validators, penalize workers, slash bonds, or write
economic ledger rows.
"""

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import validators as validators_svc
from .stats import _active_workers

router = APIRouter()


def _capabilities_payload() -> dict[str, Any]:
    return {
        "validator_api_version": "v1-preview",
        "mode": "assignment_bound_evidence",
        "economic_effect": "none",
        "features": {
            "attest": True,
            "worker_inventory": True,
            "assignments": True,
            "targeted_probe": True,
            "worker_scorecards": True,
            "assignment_health": True,
            "quorum": True,
            "validator_rewards": False,
            "staking_required": False,
            "epoch_roots": False,
        },
        "targeted_probe_enabled": True,
        "authority_model": {
            "preview": "model-routed/local evidence; visible but non-authoritative",
            "authoritative": "requires Grid-issued assignment_id + grid_nonce + probe evidence hash",
            "quorum_lifecycle": ["pending", "accepted", "disputed", "finalized"],
        },
        "endpoints": {
            "assignments": {
                "enabled": True,
                "method": "GET",
                "path": "/v1/validator/assignments",
                "auth": "v2_account_key",
                "economic_effect": "none",
            },
            "targeted_probe": {
                "enabled": True,
                "method": "POST",
                "path": "/v1/validator/probe/{assignment_id}",
                "auth": "v2_account_key",
                "economic_effect": "none",
            },
            "attest": {
                "enabled": True,
                "method": "POST",
                "path": "/v1/validator/attest",
                "auth": "v2_account_key",
                "economic_effect": "none",
            },
            "worker_inventory": {
                "enabled": True,
                "method": "GET",
                "path": "/v1/validator/workers",
                "auth": "v2_account_key",
                "targeted_probe_enabled": True,
                "economic_effect": "none",
            },
            "scorecards": {
                "enabled": True,
                "method": "GET",
                "path": "/v1/validator/scorecards",
                "auth": "v2_account_key",
                "economic_effect": "none",
            },
            "assignment_health": {
                "enabled": True,
                "method": "GET",
                "path": "/v1/validator/assignments/health",
                "auth": "v2_account_key",
                "economic_effect": "none",
            },
        },
        "notes": [
            "Preview evidence remains non-authoritative.",
            "Authoritative evidence must match a Grid-issued assignment id, nonce, and probe evidence hash.",
            "Failed validator evidence does not directly strike, slash, or alter payouts.",
            "Validator rewards are intentionally disabled until assignment/quorum behavior is proven.",
        ],
    }


class AttestationForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    signature: Optional[str] = None


async def _validator_user(apikey: Optional[str], authorization: Optional[str]) -> dict:
    user = await accounts_svc.authenticate(extract_api_key(apikey, authorization))
    if user.get("source") != "v2" or not user.get("account_id"):
        raise HTTPException(status_code=403, detail="Validator endpoints require a v2 account key.")
    return user


@router.get("/v1/validator/capabilities")
@limiter.limit("60/minute")
async def validator_capabilities(request: Request):
    """Advertise which validator surfaces this core supports."""
    return _capabilities_payload()


@router.get("/v1/validator/assignments")
@limiter.limit("30/minute")
async def validator_assignments(
    request: Request,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    limit: int = Query(5, ge=1, le=25),
    modality: str = Query("text", pattern="^(text)$"),
):
    """Return Grid-issued assignments for this validator account.

    Assignments are short-lived and carry a grid_nonce. An attestation only
    becomes authoritative if it echoes both fields and matches the target.
    """
    user = await _validator_user(apikey, authorization)
    try:
        return await validators_svc.issue_assignments(
            account_id=user["account_id"],
            validator_wallet=user.get("wallet"),
            active_workers=await _active_workers(),
            limit=limit,
            modality=modality,
        )
    except validators_svc.AssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/v1/validator/assignments/health")
@limiter.limit("30/minute")
async def validator_assignment_health(
    request: Request,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    limit: int = Query(25, ge=1, le=100),
):
    """Return assignment/quorum health for the current validator account."""
    user = await _validator_user(apikey, authorization)
    return await validators_svc.assignment_health(account_id=user["account_id"], limit=limit)


@router.post("/v1/validator/probe/{assignment_id}")
@limiter.limit("20/minute")
async def validator_probe(
    assignment_id: str,
    request: Request,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Run a hard-targeted probe for one Grid-issued assignment."""
    user = await _validator_user(apikey, authorization)
    try:
        result = await validators_svc.probe_assignment(
            account_id=user["account_id"],
            assignment_id=assignment_id,
        )
    except validators_svc.AssignmentError as exc:
        status = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc))
    if result.get("status") == "error":
        return JSONResponse(result, status_code=int(result.get("code") or 502))
    return result


@router.post("/v1/validator/attest")
@limiter.limit("60/minute")
async def submit_attestation(
    request: Request,
    form: AttestationForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Store one validator attestation as preview or authoritative evidence."""
    user = await _validator_user(apikey, authorization)
    try:
        stored = await validators_svc.record_attestation(
            account_id=user["account_id"],
            payload=form.payload,
            signature=form.signature,
        )
    except validators_svc.AttestationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        **stored,
        "economic_effect": "none",
    }


@router.get("/v1/validator/scorecards")
@limiter.limit("30/minute")
async def validator_scorecards(
    request: Request,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    limit: int = Query(100, ge=1, le=500),
    since_hours: int = Query(168, ge=1, le=24 * 90),
    worker_id: Optional[str] = Query(None, max_length=64),
    model: Optional[str] = Query(None, max_length=255),
    authority: str = Query("all", pattern="^(all|preview|authoritative)$"),
):
    """Return aggregate validator evidence for scorecards.

    Rows are grouped by authority so preview evidence cannot be mistaken for
    assignment-bound evidence. Raw payloads, nonces, signatures, account IDs,
    and validator identities are intentionally omitted.
    """
    await _validator_user(apikey, authorization)
    return await validators_svc.scorecards(
        limit=limit,
        since_hours=since_hours,
        worker_id=worker_id,
        model=model,
        authority=authority,
    )


@router.get("/v1/validator/workers")
@limiter.limit("30/minute")
async def validator_workers(
    request: Request,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Return live worker inventory for validator discovery."""
    await _validator_user(apikey, authorization)
    workers = await _active_workers()
    out = []
    for w in workers:
        out.append({
            "worker_id": w.get("worker_id"),
            "name": w.get("name"),
            "models": w.get("models", []),
            "job_types": w.get("job_types", ["text"]),
            "api_formats": w.get("api_formats", ["openai-chat"]),
            "max_context_length": w.get("max_context_length"),
            "targetable": True,
        })
    return {
        "workers": out,
        "count": len(out),
        "targeted_probe_enabled": True,
        "capabilities": _capabilities_payload()["features"],
        "probe_endpoint": "/v1/validator/probe/{assignment_id}",
        "economic_effect": "none",
    }
