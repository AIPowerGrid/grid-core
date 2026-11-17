# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One-time trial credit for a wallet account.

A wallet costs nothing to create, so this grant cannot be made unfarmable. It
is made not worth farming instead: a small amount, once per account and once
per network address, released a little each hour out of a fixed total budget,
spendable on text only, and expiring after a few days. It is a promotional
grant (`promotions.py`), so it is drawn, held and restored like any other.

OFF by default. Turning it on needs GRID_WALLET_TRIAL_ENABLED and, before the
credit can pay for anything, the campaign id in GRID_PROMO_SPENDABLE_CAMPAIGNS
with GRID_PROMO_SPENDABLE_LIVE on. Every failure grants nothing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os

import sqlalchemy as sa

from ..auth import _get_api_key_salt
from ..safe_logging import error_type, opaque_id
from ..v2.schema import promo_grants, wallet_trial_claims
from . import promotions

logger = logging.getLogger("grid_api.wallet_trial")

ENABLED = os.getenv("GRID_WALLET_TRIAL_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
CAMPAIGN_ID = os.getenv("GRID_WALLET_TRIAL_CAMPAIGN_ID", "wallet-trial-v1")
GRANT_MICRO = int(os.getenv("GRID_WALLET_TRIAL_GRANT_MICRO", "20000"))            # $0.02
EXPIRES_DAYS = int(os.getenv("GRID_WALLET_TRIAL_EXPIRES_DAYS", "3"))
BUDGET_MICRO = int(os.getenv("GRID_WALLET_TRIAL_BUDGET_MICRO", "30000000"))       # $30 in total
HOURLY_MICRO = int(os.getenv("GRID_WALLET_TRIAL_HOURLY_MICRO", "80000"))          # $0.08 an hour

# Campaigns whose credit never funds an image or video job.
TEXT_ONLY_CAMPAIGNS = frozenset({CAMPAIGN_ID})


_campaign_ready = False


def address_hash(remote_address: str | None) -> str | None:
    """Server-keyed digest of the caller's network address, or None when the
    address is unusable. IPv6 is reduced to its /64, because one connection
    owns that whole block; loopback and private addresses mean the real caller
    is hidden behind our own proxy, so they never qualify."""
    try:
        ip = ipaddress.ip_address((remote_address or "").strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        canonical = str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    else:
        canonical = str(ip)
    return hashlib.blake2b(
        canonical.encode("utf-8"),
        key=_get_api_key_salt().encode("utf-8"),
        digest_size=32,
        person=b"aipg-trial-ip-v1",
    ).hexdigest()


def _guard(account_id, digest: str):
    async def guard(session, campaign, now):
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        released = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(promo_grants.c.amount_micro), 0)).where(
                promo_grants.c.campaign_id == campaign["id"],
                promo_grants.c.created >= hour_start,
            ),
        )
        if int(released or 0) + int(campaign["grant_micro"]) > HOURLY_MICRO:
            return "hour_exhausted"
        used = await session.scalar(
            sa.select(sa.literal(True)).where(wallet_trial_claims.c.address_hash == digest),
        )
        if used:
            return "address_used"
        await session.execute(sa.insert(wallet_trial_claims).values(
            address_hash=digest, account_id=promotions._as_uuid(account_id), created=now,
        ))
        return None

    return guard


async def grant(account_id, remote_address: str | None) -> dict:
    """Try to issue the trial credit. Returns {"status", "granted_micro", ...};
    status is "granted", "already", or the reason nothing was granted."""
    nothing = {"granted_micro": 0}
    if not ENABLED or not promotions.PROMO_ENABLED:
        return {"status": "disabled", **nothing}
    if not account_id:
        return {"status": "no_account", **nothing}
    digest = address_hash(remote_address)
    if digest is None:
        return {"status": "address_unknown", **nothing}
    global _campaign_ready
    try:
        if not _campaign_ready:
            # Refuses to run if the id already exists with other amounts: a new
            # amount, budget or expiry needs a new campaign id.
            await promotions.ensure_fixed_campaign(
                CAMPAIGN_ID,
                name="Wallet trial credit",
                grant_micro=GRANT_MICRO,
                budget_micro=BUDGET_MICRO,
                expires_days=EXPIRES_DAYS,
                eligibility={"wallet_sign_in": True, "text_only": True},
            )
            _campaign_ready = True
        result = await promotions.grant_once(account_id, CAMPAIGN_ID, guard=_guard(account_id, digest))
    except Exception as exc:
        # Includes two first sign-ins from one address racing for the claim row.
        logger.warning(
            "wallet trial grant failed; granting none account=%s error_type=%s",
            opaque_id(account_id), error_type(exc),
        )
        return {"status": "unavailable", **nothing}
    if result.get("status") == "granted":
        logger.info("wallet trial granted account=%s micro=%d", opaque_id(account_id), GRANT_MICRO)
    return result


def public(result: dict) -> dict:
    """The part of a grant result a signing-in client may see."""
    status = result.get("status")
    shown = {"status": status if status in {
        "granted", "already", "hour_exhausted", "budget_exhausted", "address_used",
    } else "unavailable"}
    if status in {"granted", "already"}:
        shown["granted_usd"] = round(int(result.get("granted_micro") or 0) / 1_000_000, 6)
        if "remaining_micro" in result:
            shown["remaining_usd"] = round(int(result["remaining_micro"]) / 1_000_000, 6)
        if result.get("expires"):
            shown["expires"] = result["expires"]
        shown["models"] = "text"
    elif status == "hour_exhausted":
        shown["retry"] = "Sign in again after the next full hour, or deposit to continue."
    return shown
