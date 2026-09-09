# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticated, read-only billed-media recovery."""

from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts, media_results

router = APIRouter()
_PRIVATE = {"Cache-Control": "no-store"}


@router.get("/v1/media/results")
@limiter.limit("60/minute")
async def recover_media(
    request: Request,
    job_id: UUID | None = Query(None),
    client_ref: str | None = Query(None, min_length=1, max_length=128),
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
    x_grid_user_token: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None),
):
    try:
        user = await accounts.authenticate(
            extract_api_key(apikey, authorization), x_grid_user_assertion,
            user_token=x_grid_user_token, required_scope="inference.submit",
        )
        if (job_id is None) == (client_ref is None):
            raise HTTPException(422, detail="Provide exactly one of job_id or client_ref")
        result = await media_results.recover(user.get("account_id"), job_id=job_id,
                                             client_ref=client_ref)
        if result is None:
            raise HTTPException(404, detail="No recoverable reservation found")
        return JSONResponse(result, headers=_PRIVATE)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=_PRIVATE)
    except media_results.AmbiguousResult:
        return JSONResponse({"detail": "Multiple jobs match; recover using a Grid job_id"},
                            status_code=409, headers=_PRIVATE)
    except Exception:
        # No raw DB errors or media URLs in the public error or logs.
        return JSONResponse({"detail": "Media recovery temporarily unavailable"},
                            status_code=503, headers=_PRIVATE)
