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
from ..services import economics
from ..services import identities as identities_svc
from ..v2.schema import api_keys as api_keys_table
from ..v2.schema import payouts as payouts_table
from ..v2.schema import workers as workers_table
from ..v2.schema import ledger as ledger_table

logger = logging.getLogger("grid_api.accounts_api")

router = APIRouter()

# ── SIWE nonce store (single-use, TTL) — Redis-backed so it works across uvicorn
# workers (an in-process dict means a nonce minted on worker A fails to verify on
# worker B). SET NX + GETDEL give atomic single-use semantics. ──
_NONCE_TTL = 300
_NONCE_PREFIX = "grid:siwe_nonce:"


async def _nonce_issue() -> str:
    from ..redis_client import get_redis
    nonce = uuid_mod.uuid4().hex
    await get_redis().set(f"{_NONCE_PREFIX}{nonce}", "1", ex=_NONCE_TTL)
    return nonce


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
            "local v=redis.call('GET',KEYS[1]); "
            "if v then redis.call('DEL',KEYS[1]) end; return v",
            1, key,
        )
    return bool(val)


class WalletVerifyForm(BaseModel):
    message: str
    signature: str
    address: str
    username: Optional[str] = None


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


class ClaimDepositForm(BaseModel):
    tx_hash: str


@router.post("/v1/accounts/wallet/nonce")
@limiter.limit("30/minute")
async def wallet_nonce(request: Request):
    return {"nonce": await _nonce_issue()}


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
    if not await _nonce_consume(nonce):
        raise HTTPException(401, detail="Invalid or expired nonce. Please retry.")

    # Bind the signed message to the EXACT canonical sign-in text (the client
    # signs this verbatim). Without this, a signature the victim made elsewhere
    # (another dApp's login, a token approval) that merely contains our nonce
    # string could be replayed here to mint their session. Requiring the exact
    # message eliminates that replay class. (Follow-up: upgrade to full EIP-4361
    # with domain/issued-at when the console/gallery clients migrate.)
    expected_message = f"Sign in to AIPG Grid\n\nNonce: {nonce}"
    if form.message != expected_message:
        raise HTTPException(401, detail="Unexpected sign-in message; refusing to verify.")

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
        key = await accounts_svc.issue_key(account["id"], label="wallet-login", is_session=True)
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
            encode_defunct(text=form.message), signature=form.signature,
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
                destination, owner, reason="wallet_link",
                merge_ref=f"wallet-link:{nonce}",
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
    else:
        result = await identities_svc.attach_identity(
            destination, "wallet", recovered, display_hint=recovered,
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
):
    """Link a wallet to a bridge-asserted Google account with proof of both."""
    if not x_grid_user_assertion:
        raise HTTPException(401, detail="Google user assertion required")
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization), x_grid_user_assertion,
    )
    if user.get("asserted_provider") != "google":
        raise HTTPException(403, detail="Wallet linking requires an asserted Google identity")
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
            encode_defunct(text=form.message), signature=form.signature,
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
                destination, owner, reason="asserted_wallet_link",
                merge_ref=f"asserted-wallet-link:{nonce}",
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc))
    else:
        result = await identities_svc.attach_identity(
            destination, "wallet", recovered, display_hint=recovered,
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
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Account creation requires the internal token")
    if not (form.username or form.email or form.oauth_sub):
        raise HTTPException(400, detail="Provide at least one of username/email/oauth_sub")

    acct, key = await accounts_svc.create_account(
        username=form.username, email=form.email, oauth_sub=form.oauth_sub
    )
    return {"account_id": acct["id"], "username": acct["username"], "api_key": key}


@router.post("/v1/accounts/bridges")
async def create_identity_bridge(
    form: CreateBridgeForm,
    x_internal_token: Optional[str] = Header(None),
):
    """Bootstrap a least-privilege first-party bridge key, returned once."""
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Internal token required")
    clean = form.label.strip()
    if not clean or len(clean) > 80:
        raise HTTPException(400, detail="Bridge label must be 1..80 characters")
    acct, key = await accounts_svc.create_account(
        username=f"{clean} identity bridge", key_label=f"bridge:{clean}",
        is_session=False, scopes=["account.read", "inference.submit", "identity.assert"],
    )
    return {"account_id": acct["id"], "api_key": key, "scopes": [
        "account.read", "inference.submit", "identity.assert",
    ]}


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
    expected = os.getenv("GRID_INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(403, detail="Internal token required")
    match = _session_match(form)
    if match is None:
        raise HTTPException(
            400, detail="Provide an authoritative identity: oauth_sub, wallet, or a verified email")
    match_field, match_val = match

    from ..v2.schema import accounts as accounts_table

    match_col = getattr(accounts_table.c, match_field)
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(accounts_table).where(match_col == match_val)
            )
        ).mappings().first()

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
                .values(revoked=True)
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
                taken = (await s2.execute(
                    sa.select(accounts_table.c.id).where(accounts_table.c.email == attach_email)
                )).first()
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


async def _require_v2(apikey: Optional[str], authorization: Optional[str],
                      user_assertion: Optional[str] = None) -> dict:
    user = await accounts_svc.authenticate(
        extract_api_key(apikey, authorization), user_assertion,
        required_scope="account.read",
    )
    if user["source"] != "v2":
        raise HTTPException(
            403, detail="Key management requires a v2 account key (legacy keys are read-only)."
        )
    return user


async def _require_session(apikey: Optional[str], authorization: Optional[str]) -> dict:
    """Gate account-admin actions (change payout wallet, issue/revoke keys) to a
    wallet-proven SESSION key. A user-issued inference key can read the account
    but cannot redirect earnings or mint/kill keys — so a leaked inference key is
    not enough to steal payouts. Sign in with your wallet to get a session key."""
    user = await _require_v2(apikey, authorization)
    if not user.get("is_session"):
        raise HTTPException(
            403,
            detail="This action needs a wallet session. Sign in with your wallet "
                   "(or the dashboard) — an inference API key can't change payout "
                   "settings or manage keys.",
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
            "aipg_bps": user.get("payout_aipg_bps")
            if user.get("payout_aipg_bps") is not None
            else economics.WORKER_AIPG_SHARE_BPS,
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
            user["account_id"], asset=form.asset, aipg_bps=form.aipg_bps
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {
        "asset": (form.asset.upper() if form.asset
                  else (user.get("payout_asset") or economics.DEFAULT_PAYOUT_ASSET)),
        "aipg_bps": (form.aipg_bps if form.aipg_bps is not None
                     else (user.get("payout_aipg_bps") if user.get("payout_aipg_bps") is not None
                           else economics.WORKER_AIPG_SHARE_BPS)),
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
        return {"payout_wallet": "", "jobs": [],
                "note": "set a payout wallet to attribute + settle your worker jobs"}
    from ..v2.schema import ledger as ledger_table

    lt = ledger_table
    async with await new_session() as session:
        rows = (
            await session.execute(
                sa.select(
                    lt.c.job_id, lt.c.worker_id, lt.c.model, lt.c.job_type,
                    lt.c.den, lt.c.output_units, lt.c.duration, lt.c.ttft,
                    lt.c.result_hash, lt.c.worker_sig, lt.c.epoch_id, lt.c.created,
                )
                .where(lt.c.wallet == wallet)
                .order_by(lt.c.created.desc())
                .limit(limit)
            )
        ).mappings().all()
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
            await session.execute(
                sa.select(
                    workers_table.c.id,
                    workers_table.c.name,
                    workers_table.c.type,
                    workers_table.c.models,
                    workers_table.c.last_seen,
                    workers_table.c.maintenance,
                ).where(workers_table.c.account_id == user["account_id"])
            )
        ).mappings().all()

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
                    .group_by(ledger_table.c.worker_id)
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
        # Aggregates over ALL periods, bucketed by status (accurate beyond the
        # row cap below).
        agg = (
            await session.execute(
                sa.select(
                    payouts_table.c.status,
                    sa.func.coalesce(sa.func.sum(payouts_table.c.aipg_amount), 0).label("aipg"),
                    sa.func.coalesce(sa.func.sum(payouts_table.c.den), 0).label("den"),
                    sa.func.count().label("n"),
                )
                .where(payouts_table.c.account_id == user["account_id"])
                .group_by(payouts_table.c.status)
            )
        ).mappings().all()
        rows = (
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
                .where(payouts_table.c.account_id == user["account_id"])
                .order_by(payouts_table.c.created.desc())
                .limit(200)
            )
        ).mappings().all()

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


@router.post("/v1/account/deposits/claim-eth")
@limiter.limit("20/minute")
async def claim_eth_deposit(
    request: Request,
    form: ClaimDepositForm,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Credit the account for a native-ETH deposit to the grid treasury.

    The user sends ETH on Base, then submits the tx hash here; the grid verifies
    the transfer (to the treasury, from the account's own wallet, enough
    confirmations) and credits the prepaid balance in USD, priced ETH→USD via the
    Chainlink feed at claim time. Idempotent on the tx hash. 503 until the grid is
    configured with a treasury (GRID_ETH_TREASURY, or the shared GRID_USDC_TREASURY).
    """
    user = await _require_v2(apikey, authorization)
    from ..services import deposits
    return await deposits.verify_and_credit_eth(form.tx_hash, user)


@router.get("/v1/account/credits")
async def get_credits(
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
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
    charging_enabled is the overall live gate (dark = nothing is charged).
    """
    user = await _require_v2(apikey, authorization, x_grid_user_assertion)
    aid = user["account_id"]
    wallet = user.get("wallet") or None
    from ..services import credits as credits_svc
    from ..services import free_credits
    from ..services import promotions

    paid = await credits_svc.get_balance(aid)
    promo_left = await promotions.available_micro(aid)
    cap = await free_credits.daily_cap_micro(aid, wallet)
    free_left = await free_credits.available_micro(aid, wallet)
    total = promo_left + free_left + paid

    def usd(m):
        return round(m / 1_000_000, 6)

    # `active` says whether each shadowed pocket can currently pay a charge.
    free_active = free_credits.FREE_ENABLED and free_credits.FREE_SPENDABLE_LIVE
    promo_active = promotions.PROMO_ENABLED and promotions.PROMO_SPENDABLE_LIVE
    spendable = paid + (free_left if free_active else 0) + (promo_left if promo_active else 0)
    return {
        "promotional": {
            "remaining_micro": promo_left,
            "remaining_usd": usd(promo_left),
            "active": promo_active,
        },
        "free": {
            "daily_cap_micro": cap,
            "remaining_micro": free_left,
            "daily_cap_usd": usd(cap),
            "remaining_usd": usd(free_left),
            "resets": "utc-midnight",
            "holder_bonus_active": cap > free_credits.FREE_DAILY_MICRO,  # AIPG-tier bonus applied?
            # False = shown for transparency but NOT spendable on paid inference yet.
            "active": free_active,
        },
        "paid": {
            "balance_micro": paid,
            "balance_usd": usd(paid),
        },
        # What can ACTUALLY cover a paid charge right now (free excluded until it's
        # in the live reserve path). This is the number a client must gate on.
        "total_spendable_micro": spendable,
        "total_spendable_usd": usd(spendable),
        # promo + daily free + paid after all shadow gates are enabled.
        "total_preview_micro": total,
        "total_preview_usd": usd(total),
        "charging_enabled": credits_svc.CHARGING_ENABLED,  # false while dark
    }


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
            .values(revoked=True)
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, detail="No such key on this account")
    return {"revoked": key_id, "count": result.rowcount}
