# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Account + API key management (v2).

Current entry points are Core-verified Google OIDC, Core-verified wallet
signatures, and app-local subjects delegated by bounded service accounts. They
receive short-lived Core tokens. Legacy internal sessions and Haidra keys remain
behind explicit transition flags only.

Key management (list/issue/revoke) authenticates with any active key on the
account. Plaintext keys are returned exactly once and never stored.
"""

import hashlib
import json
import logging
import os
import re
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError

from ..auth import extract_api_key
from ..database import new_session
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import economics
from ..services import identities as identities_svc
from ..v2.schema import accounts as accounts_table
from ..v2.schema import api_keys as api_keys_table
from ..v2.schema import ledger as ledger_table
from ..v2.schema import payouts as payouts_table
from ..v2.schema import workers as workers_table

logger = logging.getLogger("grid_api.accounts_api")

router = APIRouter()

# ── SIWE nonce store (single-use, TTL) — Redis-backed so it works across uvicorn
# workers (an in-process dict means a nonce minted on worker A fails to verify on
# worker B). SET NX + GETDEL give atomic single-use semantics. ──
_NONCE_TTL = 300
_NONCE_PREFIX = "grid:siwe_nonce:"


async def _nonce_issue(value: dict | None = None, *, nonce: str | None = None) -> str:
    from ..redis_client import get_redis

    nonce = nonce or uuid_mod.uuid4().hex
    stored = json.dumps(value, separators=(",", ":"), sort_keys=True) if value else "1"
    await get_redis().set(f"{_NONCE_PREFIX}{nonce}", stored, ex=_NONCE_TTL)
    return nonce


async def _nonce_peek(nonce: str) -> dict | None:
    if not nonce:
        return None
    from ..redis_client import get_redis

    raw = await get_redis().get(f"{_NONCE_PREFIX}{nonce}")
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"kind": "legacy"}
    return value if isinstance(value, dict) else {"kind": "legacy"}


async def _nonce_consume(nonce: str) -> bool:
    """Atomically consume a nonce; True if it was valid+unused, False otherwise."""
    if not nonce:
        return False
    from ..redis_client import get_redis

    r = get_redis()
    key = f"{_NONCE_PREFIX}{nonce}"
    # GETDEL is atomic single-use; use one Lua operation on older Redis.
    try:
        val = await r.getdel(key)
    except Exception:
        val = await r.eval(
            "local v=redis.call('GET',KEYS[1]); if v then redis.call('DEL',KEYS[1]) end; return v",
            1,
            key,
        )
    return bool(val)


class WalletVerifyForm(BaseModel):
    message: str
    signature: str
    address: str
    username: Optional[str] = None


class WalletChallengeForm(BaseModel):
    address: str
    domain: str = Field(min_length=1, max_length=255)
    uri: str = Field(min_length=1, max_length=2048)
    chain_id: int = 8453


class ServiceWalletChallengeForm(WalletChallengeForm):
    app_subject: Optional[str] = None


class ServiceWalletExchangeForm(WalletVerifyForm):
    app_subject: Optional[str] = None


class WalletLinkForm(BaseModel):
    message: str
    signature: str
    address: str


class CreateAccountForm(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    oauth_sub: Optional[str] = None


class SessionForm(BaseModel):
    oauth_sub: Optional[str] = None
    email: Optional[str] = None
    wallet: Optional[str] = None
    username: Optional[str] = None
    # True only when the caller has VERIFIED the email (e.g. a magic-link login).
    # Email is an authoritative match/login key ONLY when it is the sole identity
    # and verified — never a supplement to OAuth/SIWE (see _session_match).
    email_verified: Optional[bool] = False


def _session_match(form: "SessionForm"):
    """Pick the ONE authoritative identity to resolve a session on. Precedence:
    oauth_sub > wallet > (email iff it is the sole identifier AND verified).

    NEVER OR across fields: a secondary, caller-influenceable field — above all an
    UNVERIFIED OAuth-asserted email — must not be able to join into a *different*
    account. That is the confused-deputy / account-takeover path. Returns
    ("oauth_sub"|"wallet"|"email", value) or None when nothing authoritative is
    usable (e.g. only an unverified/supplemental email was provided)."""
    if form.oauth_sub:
        return ("oauth_sub", form.oauth_sub)
    if form.wallet:
        return ("wallet", form.wallet.lower())
    if form.email and form.email_verified:
        return ("email", form.email)
    return None


class IssueKeyForm(BaseModel):
    label: Optional[str] = None


class CreateBridgeForm(BaseModel):
    label: str
    service_id: Optional[str] = None
    allowed_providers: list[str] = Field(default_factory=lambda: ["app"])
    google_audiences: list[str] = Field(default_factory=list)
    siwe_domains: list[str] = Field(default_factory=list)
    per_request_micro: Optional[int] = None
    daily_micro: Optional[int] = None
    allow_direct_inference: bool = False


class ServiceExchangeForm(BaseModel):
    subject: str


class GoogleExchangeForm(BaseModel):
    id_token: str
    app_subject: Optional[str] = None


class BindServiceIdentityForm(BaseModel):
    subject: str
    user_token: str


class ClaimDepositForm(BaseModel):
    tx_hash: str


class CreditQuoteForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=256)
    modality: Literal["text", "image", "video", "audio", "3d"]
    prompt_tokens: int = Field(default=0, ge=0, le=2_000_000)
    max_tokens: int = Field(default=0, ge=0, le=1_000_000)
    n: int = Field(default=1, ge=1, le=16)
    seconds: Optional[float] = Field(default=None, gt=0, le=3_600)

    @model_validator(mode="after")
    def require_modality_inputs(self):
        if self.modality in {"video", "audio"} and self.seconds is None:
            raise ValueError(f"seconds is required for {self.modality} quotes")
        return self


@router.post("/v1/accounts/wallet/nonce")
@limiter.limit("30/minute")
async def wallet_nonce(request: Request):
    return {"nonce": await _nonce_issue()}


def _allowed_siwe_domain(domain: str, uri: str) -> bool:
    domain = domain.strip().lower()
    parsed = urlsplit(uri)
    if not parsed.hostname or parsed.netloc.lower() != domain:
        return False
    local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        return False
    configured = os.getenv(
        "GRID_SIWE_ALLOWED_DOMAINS",
        "console.aipowergrid.io,aipg.art,aipg.chat,aipg.music",
    )
    allowed = {item.strip().lower() for item in configured.split(",") if item.strip()}
    return domain in allowed or local


def _siwe_message(
    *,
    domain: str,
    address: str,
    uri: str,
    chain_id: int,
    nonce: str,
    issued_at: str,
    expiration_time: str,
    statement: str = "Sign in to AI Power Grid.",
) -> str:
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        f"{statement}\n\n"
        f"URI: {uri}\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        f"Expiration Time: {expiration_time}"
    )


@router.post("/v1/accounts/wallet/challenge")
@limiter.limit("30/minute")
async def wallet_challenge(request: Request, form: WalletChallengeForm):
    """Issue a domain-, address-, URI-, chain-, and time-bound EIP-4361 message."""
    if not accounts_svc.is_valid_eth_address(form.address):
        raise HTTPException(422, detail="Invalid wallet address")
    if form.chain_id != 8453:
        raise HTTPException(422, detail="Wallet sign-in requires Base chain ID 8453")
    if not _allowed_siwe_domain(form.domain, form.uri):
        raise HTTPException(422, detail="Wallet sign-in origin is not allowed")

    nonce = uuid_mod.uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=0)
    issued_at = now.isoformat().replace("+00:00", "Z")
    expiration_time = (now + timedelta(seconds=_NONCE_TTL)).isoformat().replace("+00:00", "Z")
    message = _siwe_message(
        domain=form.domain.strip().lower(),
        address=form.address,
        uri=form.uri,
        chain_id=form.chain_id,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
    )
    await _nonce_issue(
        {
            "kind": "siwe",
            "address": form.address.lower(),
            "domain": form.domain.strip().lower(),
            "uri": form.uri,
            "chain_id": form.chain_id,
            "message": message,
        },
        nonce=nonce,
    )
    return {
        "nonce": nonce,
        "message": message,
        "expires_in": _NONCE_TTL,
        "chain_id": form.chain_id,
    }


@router.post("/v1/accounts/wallet/verify")
@limiter.limit("10/minute")
async def wallet_verify(request: Request, form: WalletVerifyForm):
    """Verify a wallet signature and issue a short-lived Core user token."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        raise HTTPException(501, detail="Wallet auth unavailable (eth-account not installed)")

    m = re.search(r"Nonce: ([0-9a-fA-F]+)", form.message)
    nonce = m.group(1) if m else None
    challenge = await _nonce_peek(nonce)
    if not challenge:
        raise HTTPException(401, detail="Invalid or expired nonce. Please retry.")

    if challenge.get("kind") == "siwe":
        expected_message = challenge.get("message")
        if challenge.get("address") != form.address.lower():
            raise HTTPException(401, detail="Sign-in challenge belongs to a different wallet.")
    elif os.getenv("GRID_LEGACY_SIWE_VERIFY_ENABLED", "0").lower() in {"1", "true", "yes", "on"}:
        expected_message = f"Sign in to AIPG Grid\n\nNonce: {nonce}"
    else:
        raise HTTPException(401, detail="Legacy wallet sign-in is disabled; request a SIWE challenge.")
    if form.message != expected_message:
        raise HTTPException(401, detail="Unexpected sign-in message; refusing to verify.")

    try:
        recovered = Account.recover_message(
            encode_defunct(text=form.message),
            signature=form.signature,
        )
    except Exception:
        raise HTTPException(401, detail="Signature verification failed.")
    if recovered.lower() != form.address.lower():
        raise HTTPException(401, detail="Signature does not match the address.")
    if not await _nonce_consume(nonce):
        raise HTTPException(401, detail="Sign-in challenge was already used. Please retry.")

    wallet = recovered.lower()
    account = await accounts_svc.get_account_by_wallet(wallet)
    if account:
        acct, created = account, False
    else:
        acct, _ = await accounts_svc.create_account(
            username=form.username or f"{wallet[:6]}…{wallet[-4:]}",
            wallet=wallet,
            issue_initial_key=False,
        )
        created = True

    from ..services import user_tokens

    token = user_tokens.issue(
        acct["id"],
        audience="direct",
        scopes=accounts_svc.SESSION_SCOPES,
        auth_method="siwe",
    )
    legacy_key = None
    if os.getenv("GRID_LEGACY_SESSION_KEYS_ENABLED", "0").lower() in {"1", "true", "yes", "on"}:
        legacy_key = await accounts_svc.issue_key(
            acct["id"],
            label="wallet-login",
            is_session=True,
        )
    return {
        "account_id": str(acct["id"]),
        "wallet": wallet,
        "username": acct.get("username"),
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 900,
        "api_key": legacy_key,
        "created": created,
    }


@router.post("/v1/account/identities/wallet/link")
@limiter.limit("10/minute")
async def link_wallet(
    request: Request,
    form: WalletLinkForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Attach a wallet to the current canonical account with proof of both sides.

    The session key proves the destination account; the exact-purpose signature
    proves the wallet. If that wallet already owns a separate account, the
    tested merge path conserves balances and retires the source credentials.
    """
    user = await _require_session(apikey, authorization)
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        raise HTTPException(501, detail="Wallet auth unavailable (eth-account not installed)")

    match = re.fullmatch(
        r"Link wallet to AIPG Grid account ([0-9a-fA-F-]{36})\n\nNonce: ([0-9a-fA-F]+)",
        form.message,
    )
    if not match or match.group(1).lower() != str(user["account_id"]).lower():
        raise HTTPException(401, detail="Unexpected wallet-link message")
    nonce = match.group(2)
    if not await _nonce_consume(nonce):
        raise HTTPException(401, detail="Invalid or expired nonce. Please retry.")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=form.message),
            signature=form.signature,
        ).lower()
    except Exception:
        raise HTTPException(401, detail="Signature verification failed")
    if recovered != form.address.lower() or not accounts_svc.is_valid_eth_address(recovered):
        raise HTTPException(401, detail="Signature does not match a valid wallet")

    owner = await identities_svc.resolve_identity("wallet", recovered)
    destination = user["account_id"]
    if owner and str(owner) != str(destination):
        try:
            result = await identities_svc.merge_accounts(
                destination,
                owner,
                reason="wallet_link",
                merge_ref=f"wallet-link:{nonce}",
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
    else:
        result = await identities_svc.attach_identity(
            destination,
            "wallet",
            recovered,
            display_hint=recovered,
            ref=f"wallet-link:{nonce}",
        )
    return {**result, "wallet": recovered}


@router.post("/v1/account/identities/wallet/link/asserted")
@limiter.limit("10/minute")
async def link_wallet_from_assertion(
    request: Request,
    form: WalletLinkForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
    x_grid_user_token: Optional[str] = Header(None),
):
    """Link a wallet to a delegated user account with proof of both."""
    if not (x_grid_user_assertion or x_grid_user_token):
        raise HTTPException(401, detail="Delegated Grid user token required")
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization),
        x_grid_user_assertion,
        user_token=x_grid_user_token,
    )
    if user.get("key_kind") not in {"delegated_user", "user_token"}:
        raise HTTPException(403, detail="Wallet linking requires a native Grid user token")
    from ..services import user_tokens

    user_tokens.require_recent_step_up(user.get("token_claims") or {})
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        raise HTTPException(501, detail="Wallet auth unavailable (eth-account not installed)")

    match = re.fullmatch(
        r"Link wallet to AIPG Grid identity\n\nNonce: ([0-9a-fA-F]+)",
        form.message,
    )
    if not match or not await _nonce_consume(match.group(1)):
        raise HTTPException(401, detail="Invalid or expired wallet-link nonce")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=form.message),
            signature=form.signature,
        ).lower()
    except Exception:
        raise HTTPException(401, detail="Signature verification failed")
    if recovered != form.address.lower() or not accounts_svc.is_valid_eth_address(recovered):
        raise HTTPException(401, detail="Signature does not match a valid wallet")

    owner = await identities_svc.resolve_identity("wallet", recovered)
    destination = user["account_id"]
    nonce = match.group(1)
    if owner and str(owner) != str(destination):
        try:
            result = await identities_svc.merge_accounts(
                destination,
                owner,
                reason="asserted_wallet_link",
                merge_ref=f"asserted-wallet-link:{nonce}",
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
    else:
        result = await identities_svc.attach_identity(
            destination,
            "wallet",
            recovered,
            display_hint=recovered,
            ref=f"asserted-wallet-link:{nonce}",
        )
    return {**result, "wallet": recovered}


@router.post("/v1/accounts")
async def create_account(
    form: CreateAccountForm,
    x_internal_token: Optional[str] = Header(None),
):
    """Dashboard-only account creation (email/OAuth users).

    Requires GRID_INTERNAL_TOKEN; the dashboard verifies the user's email or
    OAuth identity itself and calls this with the result.
    """
    if os.getenv("GRID_LEGACY_INTERNAL_SESSION_ENABLED", "0").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(410, detail="Legacy internal account creation is retired")
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Account creation requires the internal token")
    if not (form.username or form.email or form.oauth_sub):
        raise HTTPException(400, detail="Provide at least one of username/email/oauth_sub")

    acct, key = await accounts_svc.create_account(
        username=form.username,
        email=form.email,
        oauth_sub=form.oauth_sub,
    )
    return {"account_id": acct["id"], "username": acct["username"], "api_key": key}


@router.post("/v1/accounts/bridges")
async def create_identity_bridge(
    form: CreateBridgeForm,
    x_internal_token: Optional[str] = Header(None),
):
    """Bootstrap a bounded service account. Disable after provisioning."""
    expected = os.getenv("GRID_SERVICE_BOOTSTRAP_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Service bootstrap token required")
    clean = form.label.strip()
    if not clean or len(clean) > 80:
        raise HTTPException(400, detail="Bridge label must be 1..80 characters")
    sid = form.service_id or re.sub(r"[^a-z0-9-]+", "-", clean.lower()).strip("-")
    try:
        service, key = await accounts_svc.create_service_client(
            sid,
            clean,
            allowed_providers=form.allowed_providers,
            google_audiences=form.google_audiences,
            siwe_domains=form.siwe_domains,
            per_request_micro=form.per_request_micro,
            daily_micro=form.daily_micro,
            allow_direct_inference=form.allow_direct_inference,
        )
    except (ValueError, IntegrityError) as exc:
        raise HTTPException(409, detail=str(exc))
    return {
        "service_id": service["id"],
        "account_id": str(service["account_id"]),
        "api_key": key,
        "scopes": accounts_svc.service_scopes(
            allow_direct_inference=form.allow_direct_inference,
        ),
    }


async def _require_service_exchange(
    apikey: Optional[str],
    authorization: Optional[str],
) -> dict:
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization),
        required_scope="identity.exchange",
    )
    if user.get("key_kind") != "service" or not user.get("service_id"):
        raise HTTPException(403, detail="A service-account key is required")
    return user


def _clean_app_subject(value: str | None, *, required: bool = False) -> str | None:
    subject = (value or "").strip()
    if not subject:
        if required:
            raise HTTPException(400, detail="subject must be 1..200 characters")
        return None
    if len(subject) > 200 or any(ord(char) < 32 for char in subject):
        raise HTTPException(400, detail="subject must be 1..200 printable characters")
    return subject


async def _resolve_service_app_identity(
    service: dict,
    app_subject: str,
) -> tuple[str, object | None]:
    """Resolve the stable service namespace and absorb the legacy UUID namespace."""
    from ..services import service_auth

    primary = f"{service['service_id']}:{app_subject}"
    legacy = f"{service['account_id']}:{app_subject}"
    primary_owner = await identities_svc.resolve_identity("app", primary)
    legacy_owner = (
        await identities_svc.resolve_identity("app", legacy)
        if legacy != primary
        else None
    )
    if primary_owner and legacy_owner and str(primary_owner) != str(legacy_owner):
        try:
            await identities_svc.merge_accounts(
                primary_owner,
                legacy_owner,
                reason="service_namespace_migration",
                merge_ref=service_auth.new_event_ref(
                    "service-namespace-merge",
                    service["service_id"],
                ),
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
        primary_owner = await identities_svc.canonical_account_id(primary_owner)
    elif legacy_owner and not primary_owner:
        linked = await identities_svc.attach_identity(
            legacy_owner,
            "app",
            primary,
            display_hint=f"{service['service_id']} account",
            ref=service_auth.new_event_ref(
                "service-namespace-link",
                service["service_id"],
            ),
        )
        if linked["status"] == "conflict":
            primary_owner = await identities_svc.resolve_identity("app", primary)
        else:
            primary_owner = legacy_owner
    return primary, primary_owner


@router.post("/v1/auth/service/exchange")
@limiter.limit("120/minute")
async def exchange_service_identity(
    request: Request,
    form: ServiceExchangeForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Exchange one service-local authenticated subject for a short user token."""
    service = await _require_service_exchange(apikey, authorization)
    if "app" not in set(service.get("allowed_providers") or []):
        raise HTTPException(403, detail="This service cannot delegate app identities")
    subject = _clean_app_subject(form.subject, required=True)
    namespaced, account_id = await _resolve_service_app_identity(service, subject)
    if account_id is None:
        try:
            account, _ = await accounts_svc.create_account(
                username=f"{service['service_id']} user",
                issue_initial_key=False,
                identity_kind="app",
                identity_subject=namespaced,
            )
            account_id = account["id"]
        except IntegrityError:
            account_id = await identities_svc.resolve_identity("app", namespaced)
            if account_id is None:
                raise HTTPException(409, detail="Service identity creation conflicted")
    from ..services import service_auth

    token = service_auth.issue_user_token(
        account_id,
        service_id=service["service_id"],
        auth_method="app",
    )
    await service_auth.record_event(
        service["service_id"],
        "app_exchange",
        account_id=account_id,
        ref=service_auth.new_event_ref("exchange", service["service_id"]),
    )
    return {"access_token": token, "token_type": "Bearer", "expires_in": 900, "account_id": str(account_id)}


@router.post("/v1/auth/wallet/challenge")
@limiter.limit("30/minute")
async def exchange_wallet_challenge(
    request: Request,
    form: ServiceWalletChallengeForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Issue a partner-, app-subject-, origin-, and wallet-bound SIWE challenge."""
    service = await _require_service_exchange(apikey, authorization)
    if "wallet" not in set(service.get("allowed_providers") or []):
        raise HTTPException(403, detail="This service cannot exchange wallet identities")
    domain = form.domain.strip().lower()
    if domain not in set(service.get("siwe_domains") or []):
        raise HTTPException(403, detail="Wallet sign-in domain is not allowed for this service")
    if not accounts_svc.is_valid_eth_address(form.address):
        raise HTTPException(422, detail="Invalid wallet address")
    if form.chain_id != 8453:
        raise HTTPException(422, detail="Wallet sign-in requires Base chain ID 8453")
    if not _allowed_siwe_domain(domain, form.uri):
        raise HTTPException(422, detail="Wallet sign-in origin is not allowed")

    app_subject = _clean_app_subject(form.app_subject)
    subject_hash = hashlib.sha256(app_subject.encode()).hexdigest() if app_subject else None
    nonce = uuid_mod.uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=0)
    issued_at = now.isoformat().replace("+00:00", "Z")
    expiration_time = (now + timedelta(seconds=_NONCE_TTL)).isoformat().replace("+00:00", "Z")
    message = _siwe_message(
        domain=domain,
        address=form.address,
        uri=form.uri,
        chain_id=form.chain_id,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
        statement=f"Sign in to AI Power Grid through {service['service_id']}.",
    )
    await _nonce_issue(
        {
            "kind": "service_siwe",
            "service_id": service["service_id"],
            "app_subject_hash": subject_hash,
            "address": form.address.lower(),
            "domain": domain,
            "uri": form.uri,
            "chain_id": form.chain_id,
            "message": message,
        },
        nonce=nonce,
    )
    return {
        "nonce": nonce,
        "message": message,
        "expires_in": _NONCE_TTL,
        "chain_id": form.chain_id,
    }


@router.post("/v1/auth/wallet/exchange")
@limiter.limit("10/minute")
async def exchange_wallet_identity(
    request: Request,
    form: ServiceWalletExchangeForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Verify partner SIWE, merge the app identity, and issue a canonical token."""
    service = await _require_service_exchange(apikey, authorization)
    if "wallet" not in set(service.get("allowed_providers") or []):
        raise HTTPException(403, detail="This service cannot exchange wallet identities")
    app_subject = _clean_app_subject(form.app_subject)
    match = re.search(r"\nNonce: ([0-9a-fA-F]{32})\n", form.message)
    nonce = match.group(1) if match else None
    challenge = await _nonce_peek(nonce)
    subject_hash = hashlib.sha256(app_subject.encode()).hexdigest() if app_subject else None
    if (
        not challenge
        or challenge.get("kind") != "service_siwe"
        or challenge.get("service_id") != service["service_id"]
        or challenge.get("app_subject_hash") != subject_hash
        or challenge.get("address") != form.address.lower()
        or challenge.get("message") != form.message
    ):
        raise HTTPException(401, detail="Invalid or mismatched wallet challenge")
    from ..services.wallet_proofs import verify_personal_signature

    recovered = form.address.lower()
    if (
        not accounts_svc.is_valid_eth_address(recovered)
        or not await verify_personal_signature(
            message=form.message,
            signature=form.signature,
            address=recovered,
        )
    ):
        raise HTTPException(401, detail="Signature does not match a valid wallet")
    if not await _nonce_consume(nonce):
        raise HTTPException(401, detail="Wallet challenge was already used")

    if app_subject:
        namespaced, app_owner = await _resolve_service_app_identity(
            service,
            app_subject,
        )
    else:
        namespaced, app_owner = None, None
    wallet_owner = await identities_svc.resolve_identity("wallet", recovered)

    if app_owner:
        account_id = app_owner
        if wallet_owner and str(wallet_owner) != str(account_id):
            try:
                await identities_svc.merge_accounts(
                    account_id,
                    wallet_owner,
                    reason="service_siwe",
                    merge_ref=f"service-siwe:{service['service_id']}:{nonce}",
                )
            except ValueError as exc:
                raise HTTPException(409, detail=str(exc))
        elif not wallet_owner:
            await identities_svc.attach_identity(
                account_id,
                "wallet",
                recovered,
                display_hint=recovered,
                ref=f"service-siwe-wallet:{service['service_id']}:{nonce}",
            )
    elif wallet_owner:
        account_id = wallet_owner
    else:
        account, _ = await accounts_svc.create_account(
            username=form.username or f"{recovered[:6]}…{recovered[-4:]}",
            wallet=recovered,
            issue_initial_key=False,
        )
        account_id = account["id"]

    account_id = await identities_svc.canonical_account_id(account_id)
    if namespaced and not app_owner:
        linked = await identities_svc.attach_identity(
            account_id,
            "app",
            namespaced,
            display_hint=f"{service['service_id']} account",
            ref=f"service-siwe-app:{service['service_id']}:{nonce}",
        )
        if linked["status"] == "conflict":
            raise HTTPException(409, detail="Application identity is already linked")

    from ..services import service_auth

    token = service_auth.issue_user_token(
        account_id,
        service_id=service["service_id"],
        auth_method="siwe",
        account_manage=True,
    )
    await service_auth.record_event(
        service["service_id"],
        "wallet_exchange",
        account_id=account_id,
        ref=f"wallet-exchange:{service['service_id']}:{nonce}",
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 900,
        "account_id": str(account_id),
        "wallet": recovered,
    }


@router.post("/v1/auth/google/exchange")
@limiter.limit("30/minute")
async def exchange_google_identity(
    request: Request,
    form: GoogleExchangeForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Verify Google with Google, then issue a short Core account token."""
    service = await _require_service_exchange(apikey, authorization)
    if "google" not in set(service.get("allowed_providers") or []):
        raise HTTPException(403, detail="This service cannot exchange Google identities")
    from ..services import service_auth

    proof = await service_auth.verify_google_id_token(
        form.id_token,
        service.get("google_audiences") or [],
    )
    account_id = await identities_svc.resolve_identity("google", proof["subject"])
    if account_id is None:
        try:
            account, _ = await accounts_svc.create_account(
                username=proof.get("name") or "Google user",
                oauth_sub=proof["subject"],
                email=proof.get("email") if proof.get("email_verified") else None,
                email_verified=proof.get("email_verified", False),
                issue_initial_key=False,
                grant_verified_welcome=True,
            )
            account_id = account["id"]
        except IntegrityError:
            account_id = await identities_svc.resolve_identity("google", proof["subject"])
            if account_id is None:
                raise HTTPException(409, detail="Google identity creation conflicted")

    from ..services import promotions

    await promotions.ensure_builtin_campaign()
    await promotions.grant_once(account_id)

    app_subject = _clean_app_subject(form.app_subject)
    if app_subject:
        namespaced, owner = await _resolve_service_app_identity(
            service,
            app_subject,
        )
        if owner and str(owner) != str(account_id):
            try:
                await identities_svc.merge_accounts(
                    account_id,
                    owner,
                    reason="google_proof",
                    merge_ref=service_auth.new_event_ref("google-link", service["service_id"]),
                )
            except ValueError as exc:
                raise HTTPException(409, detail=str(exc))
        elif not owner:
            linked = await identities_svc.attach_identity(
                account_id,
                "app",
                namespaced,
                display_hint=f"{service['service_id']} account",
                ref=service_auth.new_event_ref("app-link", service["service_id"]),
            )
            if linked["status"] == "conflict":
                raise HTTPException(409, detail="Application identity is already linked")

    account_id = await identities_svc.canonical_account_id(account_id)
    token = service_auth.issue_user_token(
        account_id,
        service_id=service["service_id"],
        auth_method="google",
        account_manage=True,
    )
    await service_auth.record_event(
        service["service_id"],
        "google_exchange",
        account_id=account_id,
        ref=service_auth.new_event_ref("exchange", service["service_id"]),
    )
    return {"access_token": token, "token_type": "Bearer", "expires_in": 900, "account_id": str(account_id)}


@router.post("/v1/auth/service/bind")
@limiter.limit("20/minute")
async def bind_service_identity(
    request: Request,
    form: BindServiceIdentityForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Bind a service-local subject after fresh Core Google/SIWE proof."""
    service = await _require_service_exchange(apikey, authorization)
    if "app" not in set(service.get("allowed_providers") or []):
        raise HTTPException(403, detail="This service cannot bind app identities")
    from ..services import service_auth, user_tokens

    try:
        proof = user_tokens.verify(form.user_token, audience="direct")
    except HTTPException:
        proof = user_tokens.verify(
            form.user_token,
            audience=service["service_id"],
        )
        if proof.get("service_id") != service["service_id"]:
            raise HTTPException(401, detail="Grid user token service mismatch")
    user_tokens.require_recent_step_up(proof)
    destination = await identities_svc.canonical_account_id(proof["sub"])
    subject = form.subject.strip()
    if not subject or len(subject) > 200:
        raise HTTPException(400, detail="subject must be 1..200 characters")
    namespaced, owner = await _resolve_service_app_identity(service, subject)
    if owner and str(owner) != str(destination):
        try:
            result = await identities_svc.merge_accounts(
                destination,
                owner,
                reason="service_bind",
                merge_ref=service_auth.new_event_ref("service-bind", service["service_id"]),
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
    elif owner:
        result = {"status": "already", "account_id": str(destination)}
    else:
        result = await identities_svc.attach_identity(
            destination,
            "app",
            namespaced,
            display_hint=f"{service['service_id']} account",
            ref=service_auth.new_event_ref("service-bind", service["service_id"]),
        )
    await service_auth.record_event(
        service["service_id"],
        "service_identity_bound",
        account_id=destination,
        ref=service_auth.new_event_ref("bind", service["service_id"]),
    )
    return result


@router.post("/v1/accounts/session")
async def account_session(
    form: SessionForm,
    x_internal_token: Optional[str] = Header(None),
):
    """Dashboard login hook: find-or-create the account, rotate its
    dashboard-session key, return the fresh key.

    Internal-token gated (the dashboard verified the user via OAuth/wallet
    itself). Exactly one active "dashboard-session" key exists per account —
    each login revokes the previous one, so a leaked old session key is dead
    the moment the user logs in again.
    """
    if os.getenv("GRID_LEGACY_INTERNAL_SESSION_ENABLED", "0").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(410, detail="Legacy internal sessions are retired; use native auth exchange")
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Internal token required")
    match = _session_match(form)
    if match is None:
        raise HTTPException(400, detail="Provide an authoritative identity: oauth_sub, wallet, or a verified email")
    match_field, match_val = match

    if match_field == "oauth_sub":
        identity_kind = "github" if match_val.lower().startswith("github_") else "google"
        account_id = await identities_svc.resolve_identity(identity_kind, match_val)
    elif match_field == "wallet":
        account_id = await identities_svc.resolve_identity("wallet", match_val)
    else:
        account_id = await identities_svc.resolve_identity("email", match_val)
        if account_id is None:
            # A freshly verified magic-link may upgrade imported contact data.
            async with await new_session() as session:
                account_id = await session.scalar(
                    sa.select(accounts_table.c.id).where(accounts_table.c.email == match_val),
                )
            if account_id:
                await identities_svc.attach_identity(
                    account_id,
                    "email",
                    match_val,
                    display_hint=match_val,
                    ref=f"legacy-email-verify:{uuid_mod.uuid4()}",
                )
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(accounts_table).where(accounts_table.c.id == account_id),
                )
            )
            .mappings()
            .first()
            if account_id
            else None
        )

    created = False
    if row:
        account_id, username = row["id"], row["username"]
        # Rotate: revoke any previous dashboard-session key.
        async with await new_session() as session:
            await session.execute(
                sa.update(api_keys_table)
                .where(
                    api_keys_table.c.account_id == account_id,
                    api_keys_table.c.label == "dashboard-session",
                    api_keys_table.c.revoked.is_(False),
                )
                .values(revoked=True),
            )
            await session.commit()
        key = await accounts_svc.issue_key(account_id, label="dashboard-session", is_session=True)
    else:
        created = True
        # Attach the email for display/receipts, but ONLY if no OTHER account
        # already owns it (email is UNIQUE). Never merge, never crash on a
        # collision — and since email is not a login/match key here (see
        # _session_match), storing an unverified one can't be used to hijack.
        attach_email = form.email
        if attach_email:
            async with await new_session() as s2:
                taken = (
                    await s2.execute(
                        sa.select(accounts_table.c.id).where(accounts_table.c.email == attach_email),
                    )
                ).first()
            if taken:
                attach_email = None  # owned elsewhere — drop it, don't merge
        acct, key = await accounts_svc.create_account(
            username=form.username,
            email=attach_email,
            oauth_sub=form.oauth_sub,
            wallet=form.wallet,
            key_label="dashboard-session",
        )
        account_id, username = acct["id"], acct["username"]

    return {
        "account_id": str(account_id),
        "username": username,
        "created": created,
        "api_key": key,
    }


# ── Self-service (any active key on the account) ──


async def _require_v2(
    apikey: Optional[str], authorization: Optional[str], user_assertion: Optional[str] = None, user_token: Optional[str] = None,
) -> dict:
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization),
        user_assertion,
        user_token=user_token,
        required_scope="account.read",
    )
    if user["source"] != "v2":
        raise HTTPException(
            403,
            detail="Key management requires a v2 account key (legacy keys are read-only).",
        )
    return user


async def _require_session(apikey: Optional[str], authorization: Optional[str]) -> dict:
    """Gate account-admin actions (change payout wallet, issue/revoke keys) to a
    wallet-proven SESSION key. A user-issued inference key can read the account
    but cannot redirect earnings or mint/kill keys — so a leaked inference key is
    not enough to steal payouts. Sign in with your wallet to get a session key."""
    user = await _require_v2(apikey, authorization)
    if user.get("key_kind") in {"user_token", "delegated_user"}:
        from ..services import user_tokens

        if "account.manage" not in set(user.get("scopes") or []):
            raise HTTPException(403, detail="This user token cannot manage the account")
        user_tokens.require_recent_step_up(user.get("token_claims") or {})
        return user
    legacy_allowed = os.getenv("GRID_LEGACY_SESSION_KEYS_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not legacy_allowed or not user.get("is_session"):
        raise HTTPException(
            403,
            detail="This action needs a fresh Google or wallet proof; an inference or service key cannot manage the account.",
        )
    return user


@router.get("/v1/account")
async def get_account(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    user = await _require_v2(apikey, authorization)
    async with await new_session() as session:
        keys = (
            (
                await session.execute(
                    sa.select(
                        api_keys_table.c.hash,
                        api_keys_table.c.label,
                        api_keys_table.c.created,
                        api_keys_table.c.last_used,
                        api_keys_table.c.revoked,
                    ).where(api_keys_table.c.account_id == user["account_id"]),
                )
            )
            .mappings()
            .all()
        )
    linked_identities = await identities_svc.list_identities(user["account_id"])
    return {
        "account_id": str(user["account_id"]),
        "username": user["username"],
        "wallet": user["wallet"],
        "payout_wallet": user.get("payout_wallet") or "",
        "identities": [
            {
                "kind": identity["kind"],
                "display_hint": identity["display_hint"],
                "primary": identity["is_primary"],
                "verified": identity["verified_at"] is not None,
            }
            for identity in linked_identities
        ],
        # Worker payout preference, resolved (NULL prefs fall back to grid
        # defaults) + the option metadata the dashboard renders the picker from.
        "payout": {
            "asset": user.get("payout_asset") or economics.DEFAULT_PAYOUT_ASSET,
            "aipg_bps": user.get("payout_aipg_bps") if user.get("payout_aipg_bps") is not None else economics.WORKER_AIPG_SHARE_BPS,
            "assets": list(economics.PAYOUT_ASSETS),
            "par_assets": list(economics.PAYOUT_PAR_ASSETS),
            "conversion_fee_bps": economics.PAYOUT_CONVERSION_FEE_BPS,
            # Is the preference actually honored by the payout rail yet? Until the
            # P2 swap ships, no — the live rail settles a fixed AIPG budget by den,
            # so clients must not imply USDC/ETH/USDS payouts. `live_asset` is what
            # actually pays today.
            "active": economics.PAYOUT_ASSET_ROUTING_ENABLED,
            "live_asset": "AIPG",
        },
        "keys": [
            {
                # Identify keys by hash prefix only — enough to manage, useless to forge.
                "id": k["hash"][:12],
                "label": k["label"],
                "created": k["created"].isoformat() if k["created"] else None,
                "last_used": k["last_used"].isoformat() if k["last_used"] else None,
                "revoked": k["revoked"],
            }
            for k in keys
        ],
    }


class PayoutWalletForm(BaseModel):
    # Empty string / null clears the payout address.
    wallet: Optional[str] = None


class PayoutPreferenceForm(BaseModel):
    # Which asset to be paid in (USDC/USDS/ETH/AIPG) and/or the AIPG-slice
    # override (bps). Only the provided fields change.
    asset: Optional[str] = None
    aipg_bps: Optional[int] = None


@router.post("/v1/account/payout-wallet")
@limiter.limit("20/minute")
async def set_payout_wallet(
    request: Request,
    form: PayoutWalletForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Set the Base address worker earnings are paid to. No ownership proof
    (mining-style — point earnings wherever you want); the address is only
    format-checked. Distinct from the login wallet, so an OAuth/username
    operator can receive payouts."""
    user = await _require_session(apikey, authorization)
    try:
        value = await accounts_svc.set_payout_wallet(user["account_id"], form.wallet)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"payout_wallet": value or ""}


@router.post("/v1/account/payout-preference")
@limiter.limit("20/minute")
async def set_payout_preference(
    request: Request,
    form: PayoutPreferenceForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Set the worker's payout asset (USDC/USDS/ETH/AIPG) and/or AIPG-slice
    override. Session-gated — a leaked inference key must not be able to change
    HOW you're paid, same as payout-wallet."""
    user = await _require_session(apikey, authorization)
    try:
        await accounts_svc.set_payout_preference(
            user["account_id"],
            asset=form.asset,
            aipg_bps=form.aipg_bps,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {
        "asset": (form.asset.upper() if form.asset else (user.get("payout_asset") or economics.DEFAULT_PAYOUT_ASSET)),
        "aipg_bps": (
            form.aipg_bps
            if form.aipg_bps is not None
            else (user.get("payout_aipg_bps") if user.get("payout_aipg_bps") is not None else economics.WORKER_AIPG_SHARE_BPS)
        ),
    }


@router.get("/v1/account/jobs")
async def get_account_jobs(
    limit: int = 50,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """The caller's own worker jobs — the operator trust view. What my workers
    served, the den each earned, its output commitment, and whether it was
    signed. Scoped to my payout wallet (the same key settlement pays against, so
    this can't disagree with what I'm owed). Same privacy rule as the public
    feed: no prompt/result content, only hashes."""
    user = await _require_v2(apikey, authorization)
    wallet = (user.get("payout_wallet") or "").lower()
    limit = max(1, min(limit, 100))
    if not wallet:
        return {"payout_wallet": "", "jobs": [], "note": "set a payout wallet to attribute + settle your worker jobs"}
    from ..v2.schema import ledger as ledger_table

    lt = ledger_table
    async with await new_session() as session:
        family = await identities_svc.account_family_ids(
            user["account_id"],
            session=session,
        )
        account_rows = (
            await session.execute(
                sa.select(accounts_table.c.wallet, accounts_table.c.payout_wallet).where(accounts_table.c.id.in_(family)),
            )
        ).all()
        wallets = {value.lower() for row in account_rows for value in row if value}
        if not wallets:
            return {"payout_wallet": wallet, "jobs": [], "note": "set a payout wallet to attribute + settle your worker jobs"}
        rows = (
            (
                await session.execute(
                    sa.select(
                        lt.c.job_id,
                        lt.c.worker_id,
                        lt.c.model,
                        lt.c.job_type,
                        lt.c.den,
                        lt.c.output_units,
                        lt.c.duration,
                        lt.c.ttft,
                        lt.c.result_hash,
                        lt.c.worker_sig,
                        lt.c.epoch_id,
                        lt.c.created,
                    )
                    .where(sa.func.lower(lt.c.wallet).in_(wallets))
                    .order_by(lt.c.created.desc())
                    .limit(limit),
                )
            )
            .mappings()
            .all()
        )
    return {
        "payout_wallet": wallet,
        "total_den": round(sum(float(r["den"] or 0) for r in rows), 3),
        "jobs": [
            {
                "job_id": str(r["job_id"]),
                "model": r["model"],
                "type": r["job_type"],
                "den": round(r["den"] or 0, 3),
                "output_units": r["output_units"],
                "duration_s": round(r["duration"] or 0, 2),
                "ttft_s": round(r["ttft"], 3) if r["ttft"] is not None else None,
                "result_hash": r["result_hash"],
                "signed": bool(r["worker_sig"]),
                "epoch_id": r["epoch_id"],
                "created": r["created"].isoformat() if r["created"] else None,
            }
            for r in rows
        ],
    }


@router.get("/v1/account/workers")
async def get_account_workers(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Workers registered to the signed-in account, with live online status.

    Ownership is by account_id (workers connect with just an API key). `online`
    is the live Redis presence set; den_earned/jobs_completed are the running
    counters (authoritative totals always derivable from the ledger)."""
    user = await _require_v2(apikey, authorization)
    async with await new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(
                        workers_table.c.id,
                        workers_table.c.name,
                        workers_table.c.type,
                        workers_table.c.models,
                        workers_table.c.last_seen,
                        workers_table.c.maintenance,
                    ).where(workers_table.c.account_id == user["account_id"]),
                )
            )
            .mappings()
            .all()
        )

        # Authoritative den/jobs totals from the append-only ledger. The
        # den_earned / jobs_completed COLUMNS on grid_workers were never
        # incremented (always 0 → every operator dashboard showed "0 earned"),
        # so derive the real totals from grid_ledger in one aggregate keyed by
        # worker id. This is the stated source of truth (settlement reads it too).
        worker_ids = [r["id"] for r in rows]
        led: dict = {}
        if worker_ids:
            agg = (
                await session.execute(
                    sa.select(
                        ledger_table.c.worker_id,
                        sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
                        sa.func.count().label("jobs"),
                    )
                    .where(ledger_table.c.worker_id.in_(worker_ids))
                    .group_by(ledger_table.c.worker_id),
                )
            ).all()
            led = {row.worker_id: (float(row.den or 0.0), int(row.jobs or 0)) for row in agg}

    # Live presence by worker name (same source as /v1/workers).
    online_names: set[str] = set()
    try:
        from .stats import _active_workers

        online_names = {w.get("name") for w in await _active_workers()}
    except Exception:
        logger.debug("account workers: presence lookup failed", exc_info=True)

    workers = [
        {
            "name": r["name"],
            "type": r["type"],
            "models": r["models"] or [],
            "den_earned": round(led.get(r["id"], (0.0, 0))[0], 4),
            "jobs_completed": led.get(r["id"], (0.0, 0))[1],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "maintenance": bool(r["maintenance"]),
            "online": r["name"] in online_names,
        }
        for r in rows
    ]
    return {
        "count": len(workers),
        "online": sum(1 for w in workers if w["online"]),
        "den_earned": sum(w["den_earned"] for w in workers),
        "jobs_completed": sum(w["jobs_completed"] for w in workers),
        "workers": workers,
    }


@router.get("/v1/account/payouts")
async def get_account_payouts(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Custodial payout history for the signed-in account.

    Sourced from grid_payouts (written by the hourly settlement run): what's
    been **paid** (with on-chain tx hashes as proof) and what's **accrued**
    (owed, but parked until a payout wallet is set). Same den source of truth
    as the ledger; AIPG is distributed pro-rata by den per period."""
    user = await _require_v2(apikey, authorization)
    _PAID = ("sent", "confirmed")
    async with await new_session() as session:
        family = await identities_svc.account_family_ids(
            user["account_id"],
            session=session,
        )
        # Aggregates over ALL periods, bucketed by status (accurate beyond the
        # row cap below).
        agg = (
            (
                await session.execute(
                    sa.select(
                        payouts_table.c.status,
                        sa.func.coalesce(sa.func.sum(payouts_table.c.aipg_amount), 0).label("aipg"),
                        sa.func.coalesce(sa.func.sum(payouts_table.c.den), 0).label("den"),
                        sa.func.count().label("n"),
                    )
                    .where(payouts_table.c.account_id.in_(family))
                    .group_by(payouts_table.c.status),
                )
            )
            .mappings()
            .all()
        )
        rows = (
            (
                await session.execute(
                    sa.select(
                        payouts_table.c.period_id,
                        payouts_table.c.den,
                        payouts_table.c.aipg_amount,
                        payouts_table.c.status,
                        payouts_table.c.tx_hash,
                        payouts_table.c.address,
                        payouts_table.c.created,
                        payouts_table.c.paid,
                    )
                    .where(payouts_table.c.account_id.in_(family))
                    .order_by(payouts_table.c.created.desc())
                    .limit(200),
                )
            )
            .mappings()
            .all()
        )

    by_status = {a["status"]: a for a in agg}

    def _sum_aipg(*statuses):
        return float(sum(float(by_status[s]["aipg"]) for s in statuses if s in by_status))

    return {
        "payout_wallet": user.get("payout_wallet") or "",
        "accrued_aipg": round(_sum_aipg("accrued"), 6),
        "paid_aipg": round(_sum_aipg(*_PAID), 6),
        "total_den": round(float(sum(float(a["den"]) for a in agg)), 4),
        "periods": int(sum(a["n"] for a in agg)),
        "payouts": [
            {
                "period_id": r["period_id"],
                "den": float(r["den"]) if r["den"] is not None else 0.0,
                "aipg": float(r["aipg_amount"]) if r["aipg_amount"] is not None else 0.0,
                "status": r["status"],
                # tx_hash is a real hash only for paid rows; failed rows park an
                # error string here — the UI only links paid hashes.
                "tx_hash": r["tx_hash"] if r["status"] in _PAID else None,
                "address": r["address"],
                "created": r["created"].isoformat() if r["created"] else None,
                "paid": r["paid"].isoformat() if r["paid"] else None,
            }
            for r in rows
        ],
    }


@router.post("/v1/account/deposits/claim")
@limiter.limit("20/minute")
async def claim_deposit(
    request: Request,
    form: ClaimDepositForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Credit the account for a USDC-on-Base deposit to the grid treasury.

    The user sends USDC on Base, then submits the tx hash here; the grid verifies
    the on-chain transfer (to the treasury, from the account's own wallet, enough
    confirmations) and credits the prepaid balance 1:1. Idempotent on the tx hash.
    503 until the grid is configured with a treasury address (GRID_USDC_TREASURY).
    """
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return await deposits.verify_and_credit(form.tx_hash, user)


@router.post("/v1/account/deposits/claim-aipg")
@limiter.limit("20/minute")
async def claim_aipg_deposit(
    request: Request,
    form: ClaimDepositForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Credit a direct AIPG-on-Base deposit under the current price epoch.

    AIPG funding is available only while an operator-published valuation is
    fresh. Core applies a haircut and hard transaction/account/network caps
    before atomically writing the deposit receipt and purchased credit.
    """
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return await deposits.verify_and_credit_aipg(form.tx_hash, user)


@router.post("/v1/account/deposits/claim-eth")
@limiter.limit("20/minute")
async def claim_eth_deposit(
    request: Request,
    form: ClaimDepositForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Credit the account for a native-ETH deposit to the grid treasury.

    Direct ETH is disabled by default. A tightly capped ``buffered`` pilot can
    be enabled explicitly; the target production flow swaps ETH to USDC first
    and credits the actual stablecoin received.
    """
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return await deposits.verify_and_credit_eth(form.tx_hash, user)


@router.post("/v1/account/deposits/claim-eth-converted")
@limiter.limit("20/minute")
async def claim_converted_eth_deposit(
    request: Request,
    form: ClaimDepositForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Credit actual USDC delivered by a confirmed Base ETH swap transaction."""
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return await deposits.verify_and_credit_converted_eth(form.tx_hash, user)


@router.get("/v1/account/deposits/config")
async def get_deposit_config(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Funding assets, Base addresses, limits, and non-withdrawable terms."""
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return await deposits.funding_config(user)


@router.get("/v1/account/deposits")
async def get_deposit_history(
    limit: int = 50,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Immutable Base funding receipts for the authenticated account."""
    user = await _require_v2(apikey, authorization)
    from ..services import deposits

    return {"deposits": await deposits.list_deposits(user, limit)}


@router.get("/v1/account/credits")
async def get_credits(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
    x_grid_user_token: Optional[str] = Header(None),
):
    """The account's spendable credits — what the front ends show as
    'X free today' + '$Y balance' + a top-up prompt.

    Three pockets, one USD unit (micro-USD): promotional grants (campaign-bound
    and expiring), the daily free allowance (resets UTC midnight, tiered by AIPG
    held), and purchased balance (from on-chain deposits, never expires).

    The free-first draw IS integrated into the live durable reserve path
    (authorize_request / authorize_media hold free-first with reserve/release
    semantics), gated on GRID_FREE_SPENDABLE_LIVE. `free.active` below reflects
    that flag: false → free is display-only and total_spendable = paid only;
    true → charges draw free-first and total_spendable includes it.
    charging_enabled is the effective gate for this account. charging_mode
    exposes the operator rollout state without exposing the allowlist.
    """
    user = await _require_v2(
        apikey,
        authorization,
        x_grid_user_assertion,
        x_grid_user_token,
    )
    from ..services import credits as credits_svc

    return await credits_svc.account_credit_summary(user)


@router.post("/v1/account/credits/quote")
@limiter.limit("120/minute")
async def quote_credits(
    request: Request,
    form: CreditQuoteForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
    x_grid_user_token: Optional[str] = Header(None),
):
    """Canonical balance and non-mutating pre-dispatch price estimate."""
    user = await _require_v2(
        apikey,
        authorization,
        x_grid_user_assertion,
        x_grid_user_token,
    )
    from ..services import credits as credits_svc

    return await credits_svc.quote_for_account(
        user,
        model=form.model,
        modality=form.modality,
        prompt_tokens=form.prompt_tokens,
        max_tokens=form.max_tokens,
        n=form.n,
        seconds=float(form.seconds or 0),
    )


@router.post("/v1/account/keys")
async def issue_key(
    form: IssueKeyForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    user = await _require_session(apikey, authorization)
    key = await accounts_svc.issue_key(user["account_id"], label=form.label or "")
    return {"api_key": key, "label": form.label}


@router.delete("/v1/account/keys/{key_id}")
async def revoke_key(
    key_id: str,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Revoke a key by its 12-char hash prefix (from GET /v1/account)."""
    user = await _require_session(apikey, authorization)
    async with await new_session() as session:
        result = await session.execute(
            sa.update(api_keys_table)
            .where(
                api_keys_table.c.account_id == user["account_id"],
                api_keys_table.c.hash.like(f"{key_id}%"),
            )
            .values(revoked=True),
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, detail="No such key on this account")
    return {"revoked": key_id, "count": result.rowcount}
