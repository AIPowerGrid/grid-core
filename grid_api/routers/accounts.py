# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Account + API key management (v2).

Three ways in:

1. Wallet (SIWE) — fully self-serve, the web3-native path. Sign a nonce,
   get an account + API key. Same flow as aipg.chat and the art gallery.
2. Dashboard-created (email/OAuth) — the dashboard authenticates itself with
   X-Internal-Token (GRID_INTERNAL_TOKEN) and creates accounts on behalf of
   users it verified. Disabled when the env var is unset.
3. Legacy Haidra keys — still resolve everywhere (services/accounts.py
   fallback) until the horde is decommissioned.

Key management (list/issue/revoke) authenticates with any active key on the
account. Plaintext keys are returned exactly once and never stored.
"""

import logging
import os
import re
import time
import uuid as uuid_mod
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..auth import extract_api_key
from ..database import new_session
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..v2.schema import api_keys as api_keys_table

logger = logging.getLogger("grid_api.accounts_api")

router = APIRouter()

# ── SIWE nonce store (single-use, TTL) ──
_NONCES: dict[str, float] = {}
_NONCE_TTL = 300


class WalletVerifyForm(BaseModel):
    message: str
    signature: str
    address: str
    username: Optional[str] = None


class CreateAccountForm(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    oauth_sub: Optional[str] = None


class IssueKeyForm(BaseModel):
    label: Optional[str] = None


@router.post("/v1/accounts/wallet/nonce")
@limiter.limit("30/minute")
async def wallet_nonce(request: Request):
    now = time.time()
    for n in [n for n, exp in _NONCES.items() if exp < now]:
        _NONCES.pop(n, None)
    nonce = uuid_mod.uuid4().hex
    _NONCES[nonce] = now + _NONCE_TTL
    return {"nonce": nonce}


@router.post("/v1/accounts/wallet/verify")
@limiter.limit("10/minute")
async def wallet_verify(request: Request, form: WalletVerifyForm):
    """Verify a SIWE signature; create the account if new; issue an API key.

    The recovered signer is the identity — the claimed address is only
    cross-checked. Each successful verify issues a fresh key (label
    "wallet-login"); manage/revoke via /v1/account/keys.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        raise HTTPException(501, detail="Wallet auth unavailable (eth-account not installed)")

    m = re.search(r"Nonce: ([0-9a-fA-F]+)", form.message)
    nonce = m.group(1) if m else None
    if not nonce or _NONCES.pop(nonce, 0) < time.time():
        raise HTTPException(401, detail="Invalid or expired nonce. Please retry.")

    try:
        recovered = Account.recover_message(
            encode_defunct(text=form.message), signature=form.signature
        )
    except Exception:
        raise HTTPException(401, detail="Signature verification failed.")
    if recovered.lower() != form.address.lower():
        raise HTTPException(401, detail="Signature does not match the address.")

    wallet = recovered.lower()
    account = await accounts_svc.get_account_by_wallet(wallet)
    if account:
        key = await accounts_svc.issue_key(account["id"], label="wallet-login")
        return {
            "account_id": str(account["id"]),
            "wallet": wallet,
            "username": account.get("username"),
            "api_key": key,
            "created": False,
        }

    acct, key = await accounts_svc.create_account(
        username=form.username or f"{wallet[:6]}…{wallet[-4:]}",
        wallet=wallet,
        key_label="wallet-login",
    )
    return {
        "account_id": acct["id"],
        "wallet": wallet,
        "username": acct["username"],
        "api_key": key,
        "created": True,
    }


@router.post("/v1/accounts")
async def create_account(
    form: CreateAccountForm,
    x_internal_token: Optional[str] = Header(None),
):
    """Dashboard-only account creation (email/OAuth users).

    Requires GRID_INTERNAL_TOKEN; the dashboard verifies the user's email or
    OAuth identity itself and calls this with the result.
    """
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Account creation requires the internal token")
    if not (form.username or form.email or form.oauth_sub):
        raise HTTPException(400, detail="Provide at least one of username/email/oauth_sub")

    acct, key = await accounts_svc.create_account(
        username=form.username, email=form.email, oauth_sub=form.oauth_sub
    )
    return {"account_id": acct["id"], "username": acct["username"], "api_key": key}


# ── Self-service (any active key on the account) ──


async def _require_v2(apikey: Optional[str], authorization: Optional[str]) -> dict:
    user = await accounts_svc.authenticate(extract_api_key(apikey, authorization))
    if user["source"] != "v2":
        raise HTTPException(
            403, detail="Key management requires a v2 account key (legacy keys are read-only)."
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
            await session.execute(
                sa.select(
                    api_keys_table.c.hash,
                    api_keys_table.c.label,
                    api_keys_table.c.created,
                    api_keys_table.c.last_used,
                    api_keys_table.c.revoked,
                ).where(api_keys_table.c.account_id == user["account_id"])
            )
        ).mappings().all()
    return {
        "account_id": str(user["account_id"]),
        "username": user["username"],
        "wallet": user["wallet"],
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


@router.post("/v1/account/keys")
async def issue_key(
    form: IssueKeyForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    user = await _require_v2(apikey, authorization)
    key = await accounts_svc.issue_key(user["account_id"], label=form.label or "")
    return {"api_key": key, "label": form.label}


@router.delete("/v1/account/keys/{key_id}")
async def revoke_key(
    key_id: str,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Revoke a key by its 12-char hash prefix (from GET /v1/account)."""
    user = await _require_v2(apikey, authorization)
    async with await new_session() as session:
        result = await session.execute(
            sa.update(api_keys_table)
            .where(
                api_keys_table.c.account_id == user["account_id"],
                api_keys_table.c.hash.like(f"{key_id}%"),
            )
            .values(revoked=True)
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, detail="No such key on this account")
    return {"revoked": key_id, "count": result.rowcount}
