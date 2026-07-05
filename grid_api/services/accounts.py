# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""v2 accounts: key resolution, account creation, key issuance.

Identity model (docs/V2.md): one grid_account, wallet-canonical, with API
keys as derived credentials. During the transition, key resolution checks
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
from ..v2.schema import api_keys as api_keys_table
from ..v2.schema import workers as workers_table
from .quota import PAID_KUDOS_THRESHOLD

logger = logging.getLogger("grid_api.accounts")

API_KEY_PREFIX = "grid_"


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
                    accounts_table.c.id.label("account_id"),
                    accounts_table.c.username,
                    accounts_table.c.wallet,
                    accounts_table.c.payout_wallet,
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
            flags = row["flags"] or {}
            # Best-effort usage stamp; never fail auth over it.
            try:
                await session.execute(
                    sa.update(api_keys_table)
                    .where(api_keys_table.c.hash == hashed)
                    .values(last_used=datetime.now(timezone.utc))
                )
                await session.execute(
                    sa.update(accounts_table)
                    .where(accounts_table.c.id == row["account_id"])
                    .values(last_active=datetime.now(timezone.utc))
                )
                await session.commit()
            except Exception:
                logger.debug("last_used stamp failed", exc_info=True)

            return {
                "source": "v2",
                "id": f"v2:{row['account_id']}",
                "account_id": row["account_id"],
                # True only for wallet-proven session keys — gates account-admin
                # actions (payout wallet, key management) against leaked API keys.
                "is_session": bool(row["is_session"]),
                "username": row["username"] or "",
                "wallet": row["wallet"] or "",
                # Payout address for worker earnings; falls back to the identity
                # wallet so SIWE users are paid without setting a separate one.
                "payout_wallet": row["payout_wallet"] or row["wallet"] or "",
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


async def authenticate(plain_key: str) -> dict:
    """resolve_api_key or 401."""
    user = await resolve_api_key(plain_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


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
) -> tuple[dict, str]:
    """Create a grid_account + its first API key.

    The first key is the account's LOGIN credential, so it's a session key by
    default (can manage payout wallet + keys). Returns (account dict, plaintext
    key). The key is never stored or logged.
    """
    plain = generate_api_key()
    account_id = uuid4()
    now = datetime.now(timezone.utc)
    async with await new_session() as session:
        await session.execute(
            sa.insert(accounts_table).values(
                id=account_id,
                wallet=wallet.lower() if wallet else None,
                email=email,
                oauth_sub=oauth_sub,
                username=username,
                flags={},
                created=now,
            )
        )
        await session.execute(
            sa.insert(api_keys_table).values(
                hash=hash_api_key(plain),
                account_id=account_id,
                label=key_label,
                is_session=is_session,
                created=now,
                revoked=False,
            )
        )
        await session.commit()
    logger.info(f"Account created: {account_id} (wallet={wallet or '-'})")
    return {"id": str(account_id), "username": username, "wallet": wallet}, plain


async def issue_key(account_id, label: str = "", is_session: bool = False) -> str:
    """Issue an additional API key for an account; returns plaintext once.

    is_session defaults False: keys minted here (via /v1/account/keys) are
    inference-only and CANNOT perform account-admin actions. Only the SIWE
    wallet-login / dashboard-login paths pass is_session=True.
    """
    plain = generate_api_key()
    async with await new_session() as session:
        await session.execute(
            sa.insert(api_keys_table).values(
                hash=hash_api_key(plain),
                account_id=account_id,
                label=label or None,
                is_session=is_session,
                created=datetime.now(timezone.utc),
                revoked=False,
            )
        )
        await session.commit()
    return plain


async def get_account_by_wallet(wallet: str) -> dict | None:
    async with await new_session() as session:
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
