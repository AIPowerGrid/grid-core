# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OAuth 2.1 discovery, public-client registration, and MCP authorization."""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import oauth_server, user_tokens

router = APIRouter()


class ClientRegistration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    redirect_uris: list[str]
    client_name: str = "MCP client"
    application_type: str = "native"
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code"])
    response_types: list[str] = Field(default_factory=lambda: ["code"])
    token_endpoint_auth_method: str = "none"


class AuthorizationCapability(BaseModel):
    request: str = Field(min_length=32, max_length=256)


class AuthorizationDecision(AuthorizationCapability):
    approve: bool


def _oauth_error(exc: oauth_server.OAuthProtocolError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "error_description": exc.description},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _bounded_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise oauth_server.OAuthProtocolError(
            "invalid_request",
            "Content-Type must be application/x-www-form-urlencoded",
        )
    body = await request.body()
    if len(body) > 16_384:
        raise oauth_server.OAuthProtocolError("invalid_request", "Form body is too large")
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=12,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise oauth_server.OAuthProtocolError("invalid_request", "Malformed form body") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise oauth_server.OAuthProtocolError("invalid_request", "Duplicate form fields are not allowed")
    return {key: values[0] for key, values in parsed.items()}


async def _bounded_registration(request: Request) -> ClientRegistration:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, detail="Content-Type must be application/json")
    body = await request.body()
    if len(body) > 32_768:
        raise HTTPException(413, detail="Registration body is too large")
    try:
        return ClientRegistration.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(400, detail="Invalid client registration metadata") from exc


async def _require_console_user(
    apikey: str | None,
    authorization: str | None,
    user_token: str | None,
) -> dict:
    if not user_token:
        raise HTTPException(401, detail="Console user token required")
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization),
        user_token=user_token,
        required_scope="account.read",
    )
    if user.get("key_kind") != "delegated_user" or user.get("service_id") != "grid-console":
        raise HTTPException(403, detail="The Grid Console must authorize this request")
    user_tokens.require_recent_step_up(user.get("token_claims") or {})
    return user


async def _require_mcp_service(
    apikey: str | None,
    authorization: str | None,
) -> dict:
    service = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization),
        required_scope="oauth.introspect",
    )
    if service.get("key_kind") != "service" or service.get("service_id") != oauth_server.mcp_service_id():
        raise HTTPException(403, detail="MCP resource service key required")
    return service


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata():
    oauth_server.require_enabled()
    return {
        "resource": oauth_server.resource(),
        "authorization_servers": [oauth_server.issuer()],
        "scopes_supported": sorted(oauth_server.ALLOWED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://docs.aipowergrid.io/integrations/mcp",
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    oauth_server.require_enabled()
    base = oauth_server.issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/v1/oauth/authorize",
        "token_endpoint": f"{base}/v1/oauth/token",
        "registration_endpoint": f"{base}/v1/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": sorted(oauth_server.ALLOWED_SCOPES),
        "service_documentation": "https://docs.aipowergrid.io/integrations/mcp",
    }


@router.post("/v1/oauth/register", status_code=201)
@limiter.limit("10/minute")
async def register_client(request: Request):
    oauth_server.require_enabled()
    form = await _bounded_registration(request)
    try:
        result = await oauth_server.register_client(form.model_dump())
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})


@router.get("/v1/oauth/authorize")
@limiter.limit("30/minute")
async def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(..., min_length=1, max_length=96),
    redirect_uri: str = Query(..., min_length=1, max_length=2048),
    scope: str = Query(..., min_length=1, max_length=256),
    state: str = Query(..., min_length=1, max_length=512),
    code_challenge: str = Query(..., min_length=1, max_length=128),
    code_challenge_method: str = Query(...),
    resource: str = Query(..., min_length=1, max_length=512),
):
    oauth_server.require_enabled()
    try:
        target = await oauth_server.create_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            requested_resource=resource,
        )
    except oauth_server.OAuthProtocolError as exc:
        return _oauth_error(exc)
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


@router.post("/v1/oauth/authorization/inspect")
@limiter.limit("60/minute")
async def inspect_authorization(
    request: Request,
    form: AuthorizationCapability,
    x_grid_user_token: str | None = Header(None),
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    oauth_server.require_enabled()
    await _require_console_user(apikey, authorization, x_grid_user_token)
    return await oauth_server.inspect_authorization_request(form.request)


@router.post("/v1/oauth/authorization/decision")
@limiter.limit("30/minute")
async def decide_authorization(
    request: Request,
    form: AuthorizationDecision,
    x_grid_user_token: str | None = Header(None),
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    oauth_server.require_enabled()
    user = await _require_console_user(apikey, authorization, x_grid_user_token)
    redirect_to = await oauth_server.decide_authorization_request(
        form.request,
        approve=form.approve,
        account_id=user["account_id"],
        auth_method=user.get("auth_method") or "app",
    )
    return {"redirect_to": redirect_to}


@router.post("/v1/oauth/token")
@limiter.limit("60/minute")
async def token(request: Request):
    oauth_server.require_enabled()
    try:
        form = await _bounded_form(request)
        result = await oauth_server.exchange_authorization_code(
            grant_type=form.get("grant_type", ""),
            code=form.get("code", ""),
            client_id=form.get("client_id", ""),
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
            requested_resource=form.get("resource", ""),
        )
    except oauth_server.OAuthProtocolError as exc:
        return _oauth_error(exc)
    return JSONResponse(
        result,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/v1/oauth/introspect")
@limiter.limit("120/minute")
async def introspect(
    request: Request,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
):
    oauth_server.require_enabled()
    await _require_mcp_service(apikey, authorization)
    try:
        form = await _bounded_form(request)
    except oauth_server.OAuthProtocolError as exc:
        return _oauth_error(exc)
    return oauth_server.introspect_access_token(form.get("token", ""))
