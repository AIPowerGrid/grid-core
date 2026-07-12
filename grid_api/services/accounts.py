# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""v2 accounts: key resolution, account creation, key issuance.

Identity model: one canonical Grid account with multiple independently proven
login identities and individually scoped API keys. During the transition, key resolution checks
grid_api_keys first and falls back to the legacy Haidra users table, so old
keys keep working until the horde is decommissioned.

The normalized auth dict returned by authenticate() satisfies the contracts
of the existing quota/concurrency code (id, kudos, username) regardless of
which store the key came from.
"""

import logging
import re
import secrets
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from fastapi import HTTPException

from ..auth import hash_api_key
from ..database import new_session, users_table
from ..v2.schema import accounts as accounts_table
from ..v2.schema import account_identities as identities_table
from ..v2.schema import api_keys as api_keys_table
from ..v2.schema import workers as workers_table
from .quota import PAID_KUDOS_THRESHOLD

logger = logging.getLogger("grid_api.accounts")

API_KEY_PREFIX = "grid_"
SESSION_SCOPES = ["account.read", "account.manage", "inference.submit"]
INFERENCE_SCOPES = ["account.read", "inference.submit"]


def generate_api_key() -> str:
    """New plaintext API key — shown to the owner exactly once."""
    return API_KEY_PREFIX + secrets.token_urlsafe(24)


async def resolve_api_key(plain_key: str) -> dict | None:
    """Resolve a plaintext key to a normalized auth dict, or None.

    v2 keys win; legacy Haidra users are the fallback. The dict always has:
      id          — quota/metering identity ("v2:<uuid>" or legacy int)
      source      — "v2" | "legacy"
      username    — display name
      kudos       — legacy paid-tier signal (mapped from flags.paid for v2)
      concurrency — request concurrency allowance
      wallet      — payout address if known
    """
    hashed = hash_api_key(plain_key)
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(
                    api_keys_table.c.hash,
                    api_keys_table.c.is_session,
                    api_keys_table.c.scopes,
                    accounts_table.c.id.label("account_id"),
                    accounts_table.c.username,
                    accounts_table.c.wallet,
                    accounts_table.c.payout_wallet,
                    accounts_table.c.payout_asset,
                    accounts_table.c.payout_aipg_bps,
                    accounts_table.c.flags,
                )
                .select_from(
                    api_keys_table.join(
                        accounts_table, api_keys_table.c.account_id == accounts_table.c.id
                    )
                )
                .where(
                    api_keys_table.c.hash == hashed,
                    api_keys_table.c.revoked.is_(False),
                )
            )
        ).mappings().first()

        if row:
            from .identities import canonical_account_id

            flags = row["flags"] or {}
            canonical_id = await canonical_account_id(row["account_id"], session=session)
            # Best-effort usage stamp; never fail auth over it.
            try:
                await session.execute(
                    sa.update(api_keys_table)
                    .where(api_keys_table.c.hash == hashed)
                    .values(last_used=datetime.now(timezone.utc))
                )
                await session.execute(
                    sa.update(accounts_table)
                    .where(accounts_table.c.id == canonical_id)
                    .values(last_active=datetime.now(timezone.utc))
                )
                await session.commit()
            except Exception:
                logger.debug("last_used stamp failed", exc_info=True)

            return {
                "source": "v2",
                "id": f"v2:{canonical_id}",
                "account_id": canonical_id,
                # True only for wallet-proven session keys — gates account-admin
                # actions (payout wallet, key management) against leaked API keys.
                "is_session": bool(row["is_session"]),
                "scopes": list(row["scopes"] or (
                    SESSION_SCOPES if row["is_session"] else INFERENCE_SCOPES
                )),
                "username": row["username"] or "",
                "wallet": row["wallet"] or "",
                # Payout address for worker earnings; falls back to the identity
                # wallet so SIWE users are paid without setting a separate one.
                "payout_wallet": row["payout_wallet"] or row["wallet"] or "",
                # Worker payout preference (NULL → grid defaults, resolved by the
                # payout path). Carried on the auth'd user for the settle path.
                "payout_asset": row["payout_asset"],
                "payout_aipg_bps": row["payout_aipg_bps"],
                # Legacy paid-tier signal: quota.is_paid checks kudos against
                # the threshold, so map the v2 paid flag onto it.
                "kudos": PAID_KUDOS_THRESHOLD if flags.get("paid") else 0,
                "concurrency": int(flags.get("concurrency", 30)),
            }

        legacy = (
            await session.execute(
                sa.select(users_table).where(users_table.c.api_key == hashed)
            )
        ).mappings().first()
        if legacy:
            return {**dict(legacy), "source": "legacy", "wallet": "", "payout_wallet": ""}

    return None


async def _account_auth(account_id, *, scopes: list[str] | None = None) -> dict:
    """Build a non-admin inference identity for a bridge-asserted user."""
    from .identities import canonical_account_id

    aid = await canonical_account_id(account_id)
    async with await new_session() as session:
        row = (await session.execute(
            sa.select(accounts_table).where(accounts_table.c.id == aid)
        )).mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="Asserted account no longer exists")
    flags = row["flags"] or {}
    return {
        "source": "v2", "id": f"v2:{aid}", "account_id": aid,
        "is_session": False, "scopes": list(scopes or INFERENCE_SCOPES),
        "username": row["username"] or "", "wallet": row["wallet"] or "",
        "payout_wallet": row["payout_wallet"] or row["wallet"] or "",
        "payout_asset": row["payout_asset"], "payout_aipg_bps": row["payout_aipg_bps"],
        "kudos": PAID_KUDOS_THRESHOLD if flags.get("paid") else 0,
        "concurrency": int(flags.get("concurrency", 30)),
    }


async def authenticate(plain_key: str, user_assertion: str | None = None,
                       *, required_scope: str | None = None) -> dict:
    """Authenticate a direct API key or a scoped bridge plus user assertion."""
    user = await resolve_api_key(plain_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    scopes = set(user.get("scopes") or [])
    if required_scope and user.get("source") == "v2" and required_scope not in scopes:
        raise HTTPException(status_code=403, detail=f"API key lacks {required_scope} scope")
    if not user_assertion:
        if required_scope == "inference.submit" and "identity.assert" in scopes:
            raise HTTPException(status_code=401, detail="Identity bridge requires a user assertion")
        return user

    from sqlalchemy.exc import IntegrityError
    from . import assertions
    from .identities import resolve_identity

    asserted = await assertions.verify(plain_key, user, user_assertion)
    provider, subject = asserted["provider"], asserted["subject"]
    account_id = await resolve_identity(provider, subject)
    if account_id is None:
        kwargs = {"oauth_sub": subject} if provider == "google" else {"wallet": subject}
        try:
            account, _ = await create_account(
                username="Google user" if provider == "google" else None,
                issue_initial_key=False,
                **kwargs,
            )
            account_id = account["id"]
        except IntegrityError:
            # Concurrent first requests can race the unique identity insert.
            account_id = await resolve_identity(provider, subject)
            if account_id is None:
                raise HTTPException(409, detail="Identity account creation conflicted")
    asserted_user = await _account_auth(account_id)
    asserted_user["bridge_account_id"] = user.get("account_id")
    asserted_user["asserted_provider"] = provider
    if required_scope and required_scope not in set(asserted_user.get("scopes") or []):
        raise HTTPException(status_code=403, detail=f"Asserted user lacks {required_scope} scope")
    return asserted_user


async def assert_owns_worker(user: dict, worker_name: str) -> None:
    """Authorize worker affinity: the account must OWN the named worker.

    Targeting a worker you don't own would let you steer load onto (or grief)
    another operator's hardware, so this is a hard gate. Workers are bound to the
    account that registered them (grid_workers.account_id, enforced at register).

    Raises 400 (no account context — e.g. legacy key), 404 (no such worker), or
    403 (worker owned by another account). Returns None when ownership is good.
    """
    account_id = user.get("account_id")
    if not account_id:
        # Legacy keys have no v2 account and therefore own no v2 workers.
        raise HTTPException(status_code=403, detail="Worker targeting requires a v2 account key.")
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(workers_table.c.account_id).where(workers_table.c.name == worker_name)
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No worker named '{worker_name}'.")
    if str(row[0]) != str(account_id):
        raise HTTPException(status_code=403, detail="You do not own that worker.")


async def create_account(
    *,
    username: str | None = None,
    wallet: str | None = None,
    email: str | None = None,
    oauth_sub: str | None = None,
    key_label: str = "default",
    is_session: bool = True,
    email_verified: bool = False,
    scopes: list[str] | None = None,
    issue_initial_key: bool = True,
) -> tuple[dict, str | None]:
    """Create a grid_account + its first API key.

    The first key is the account's LOGIN credential, so it's a session key by
    default (can manage payout wallet + keys). Returns (account dict, plaintext
    key). The key is never stored or logged.
    """
    plain = generate_api_key()
    account_id = uuid4()
    now = datetime.now(timezone.utc)
    wallet = wallet.lower() if wallet else None
    scopes = list(scopes or (SESSION_SCOPES if is_session else INFERENCE_SCOPES))
    async with await new_session() as session:
        await session.execute(
            sa.insert(accounts_table).values(
                id=account_id,
                wallet=wallet,
                email=email,
                oauth_sub=oauth_sub,
                username=username,
                flags={},
                created=now,
            )
        )
        if issue_initial_key:
            await session.execute(
                sa.insert(api_keys_table).values(
                    hash=hash_api_key(plain),
                    account_id=account_id,
                    label=key_label,
                    is_session=is_session,
                    scopes=scopes,
                    created=now,
                    revoked=False,
                )
            )
        identity_rows = []
        if wallet:
            identity_rows.append(("wallet", wallet, wallet, True))
        oauth_kind = "github" if oauth_sub and oauth_sub.lower().startswith("github_") else "google"
        if oauth_sub:
            identity_rows.append((oauth_kind, oauth_sub, f"{oauth_kind.title()} account", True))
        if email:
            identity_rows.append(("email", email, email, bool(email_verified)))
        if identity_rows:
            from .identities import subject_hash

            await session.execute(sa.insert(identities_table), [
                {
                    "id": uuid4(), "account_id": account_id, "kind": kind,
                    "subject_hash": subject_hash(kind, subject), "display_hint": hint,
                    "metadata": {"source": "account_create"},
                    "verified_at": now if verified else None,
                    "is_primary": True, "created": now,
                }
                for kind, subject, hint, verified in identity_rows
            ])
        await session.commit()
    # A fresh wallet is free to create and is therefore not sufficient proof for
    # promotional value. Google verification is the current strong-identity gate.
    if oauth_sub and oauth_kind == "google":
        try:
            from . import promotions

            await promotions.ensure_builtin_campaign()
            await promotions.grant_once(account_id)
        except Exception:
            logger.warning("Welcome grant failed for account %s", account_id, exc_info=True)
    logger.info(f"Account created: {account_id} (wallet={wallet or '-'})")
    return {"id": str(account_id), "username": username, "wallet": wallet}, (
        plain if issue_initial_key else None
    )


async def issue_key(account_id, label: str = "", is_session: bool = False,
                    scopes: list[str] | None = None) -> str:
    """Issue an additional API key for an account; returns plaintext once.

    is_session defaults False: keys minted here (via /v1/account/keys) are
    inference-only and CANNOT perform account-admin actions. Only the SIWE
    wallet-login / dashboard-login paths pass is_session=True.
    """
    plain = generate_api_key()
    effective_scopes = list(scopes or (SESSION_SCOPES if is_session else INFERENCE_SCOPES))
    async with await new_session() as session:
        await session.execute(
            sa.insert(api_keys_table).values(
                hash=hash_api_key(plain),
                account_id=account_id,
                label=label or None,
                is_session=is_session,
                scopes=effective_scopes,
                created=datetime.now(timezone.utc),
                revoked=False,
            )
        )
        await session.commit()
    return plain


async def get_account_by_wallet(wallet: str) -> dict | None:
    from .identities import resolve_identity

    identity_owner = await resolve_identity("wallet", wallet)
    async with await new_session() as session:
        if identity_owner:
            row = (
                await session.execute(
                    sa.select(accounts_table).where(accounts_table.c.id == identity_owner)
                )
            ).mappings().first()
            return dict(row) if row else None
        row = (
            await session.execute(
                sa.select(accounts_table).where(accounts_table.c.wallet == wallet.lower())
            )
        ).mappings().first()
        return dict(row) if row else None


_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_eth_address(addr: str) -> bool:
    """Well-formed EVM address (0x + 40 hex). Format only — like a miner's
    payout config, we don't prove control of the address."""
    return bool(addr and _ADDR_RE.match(addr.strip()))


async def set_payout_wallet(account_id, address: str | None) -> str | None:
    """Set (or clear with None/"") an account's payout address.

    No ownership proof — point earnings wherever you like, mining-style. We only
    validate the FORMAT to catch typos. Stored lowercase; returns the stored
    value. Raises ValueError on a malformed address."""
    cleaned = (address or "").strip().lower()
    if cleaned and not is_valid_eth_address(cleaned):
        raise ValueError("payout address must be a valid 0x-prefixed 40-hex EVM address")
    value = cleaned or None
    async with await new_session() as session:
        await session.execute(
            sa.update(accounts_table)
            .where(accounts_table.c.id == account_id)
            .values(payout_wallet=value)
        )
        await session.commit()
    logger.info(f"payout_wallet set: account={account_id} -> {value or '(cleared)'}")
    return value


async def set_payout_preference(account_id, *, asset=None, aipg_bps=None) -> dict:
    """Set the worker's payout asset and/or AIPG-slice override. Only the fields
    provided are changed. Validates against the allowed asset set + bps range;
    raises ValueError on bad input. Stored preference is consumed by the
    multi-asset payout path (until then it's dashboard-only)."""
    from . import economics

    vals: dict = {}
    if asset is not None:
        a = (asset or "").upper()
        if a not in economics.PAYOUT_ASSETS:
            raise ValueError(f"payout asset must be one of {list(economics.PAYOUT_ASSETS)}")
        vals["payout_asset"] = a
    if aipg_bps is not None:
        try:
            b = int(aipg_bps)
        except (TypeError, ValueError):
            raise ValueError("payout_aipg_bps must be an integer 0..10000")
        if not (0 <= b <= 10_000):
            raise ValueError("payout_aipg_bps must be between 0 and 10000")
        vals["payout_aipg_bps"] = b
    if not vals:
        return {}
    async with await new_session() as session:
        await session.execute(
            sa.update(accounts_table).where(accounts_table.c.id == account_id).values(**vals)
        )
        await session.commit()
    logger.info(f"payout preference set: account={account_id} -> {vals}")
    return vals
