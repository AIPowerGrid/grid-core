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

_config_ok = True


def _env_int(name: str, default: int) -> int:
    """A malformed amount must switch the trial off, never break sign-in."""
    global _config_ok
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.error("invalid %s; wallet trial credit disabled", name)
        _config_ok = False
        return default
    return value


CAMPAIGN_PREFIX = "wallet-trial-"
CAMPAIGN_ID = os.getenv("GRID_WALLET_TRIAL_CAMPAIGN_ID", "").strip() or "wallet-trial-v1"
GRANT_MICRO = _env_int("GRID_WALLET_TRIAL_GRANT_MICRO", 20_000)            # $0.02
EXPIRES_DAYS = _env_int("GRID_WALLET_TRIAL_EXPIRES_DAYS", 3)
BUDGET_MICRO = _env_int("GRID_WALLET_TRIAL_BUDGET_MICRO", 30_000_000)      # $30 in total
HOURLY_MICRO = _env_int("GRID_WALLET_TRIAL_HOURLY_MICRO", 80_000)          # $0.08 an hour
if not CAMPAIGN_ID.startswith(CAMPAIGN_PREFIX):
    logger.error("GRID_WALLET_TRIAL_CAMPAIGN_ID must start with %s; disabled", CAMPAIGN_PREFIX)
    _config_ok = False
ENABLED = _config_ok and os.getenv("GRID_WALLET_TRIAL_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

# Addresses of the CDN in front of Core (Cloudflare's published ranges). A
# request arriving from one of these carries the real caller in
# CF-Connecting-IP, which the CDN sets itself and a client cannot forge.
_DEFAULT_EDGE_CIDRS = (
    "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,141.101.64.0/18,"
    "108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,197.234.240.0/22,198.41.128.0/17,"
    "162.158.0.0/15,104.16.0.0/13,104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,"
    "2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,"
    "2a06:98c0::/29,2c0f:f248::/32"
)


def _parse_cidrs(raw: str) -> tuple:
    networks = []
    for part in raw.split(","):
        if part.strip():
            networks.append(ipaddress.ip_network(part.strip(), strict=False))
    return tuple(networks)


try:
    EDGE_NETWORKS = _parse_cidrs(os.getenv("GRID_WALLET_TRIAL_EDGE_CIDRS", "").strip() or _DEFAULT_EDGE_CIDRS)
except ValueError:
    logger.error("invalid GRID_WALLET_TRIAL_EDGE_CIDRS; wallet trial credit disabled")
    EDGE_NETWORKS = ()
    ENABLED = False

# One home or hosting customer usually owns more than a /64.
IPV6_PREFIX = 56


def text_only_campaigns(spendable=None) -> frozenset[str]:
    """Campaigns whose credit never funds an image, video or audio job: every
    wallet trial campaign, including earlier ids still being spent down."""
    listed = promotions.PROMO_SPENDABLE_CAMPAIGNS if spendable is None else spendable
    return frozenset(c for c in listed if c.startswith(CAMPAIGN_PREFIX)) | {CAMPAIGN_ID}


def _ip(text):
    try:
        ip = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def caller_address(request) -> str | None:
    """The address the trial is limited by. Behind the CDN the connecting peer
    is an edge node shared by unrelated callers, so the CDN's own header names
    the caller; from anywhere else that header is ignored."""
    from slowapi.util import get_remote_address

    peer = _ip(get_remote_address(request))
    if peer is None:
        return None
    if any(peer.version == net.version and peer in net for net in EDGE_NETWORKS):
        named = _ip(request.headers.get("cf-connecting-ip"))
        return str(named) if named is not None else None
    return str(peer)


_campaign_ready = False


def address_hash(remote_address: str | None) -> str | None:
    """Server-keyed digest of the caller's network address, or None when the
    address is unusable. IPv6 is reduced to its /56. Loopback and private
    addresses mean the real caller is hidden behind our own proxy, so they
    never qualify."""
    ip = _ip(remote_address)
    if ip is None or not ip.is_global:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        block = ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False)
        canonical = f"{block.network_address}/{IPV6_PREFIX}"
    else:
        canonical = str(ip)
    # blake2b keys are at most 64 bytes; the salt may be longer.
    key = hashlib.sha256(_get_api_key_salt().encode("utf-8")).digest()
    return hashlib.blake2b(
        canonical.encode("utf-8"), key=key, digest_size=32, person=b"aipg-trial-ip-v1",
    ).hexdigest()


async def _existing_grant(account_id) -> dict | None:
    """Unlocked read for the common case: a returning wallet already has it."""
    async with await promotions.new_session() as session:
        row = (await session.execute(
            sa.select(promo_grants.c.amount_micro, promo_grants.c.remaining_micro).where(
                promo_grants.c.account_id == promotions._as_uuid(account_id),
                promo_grants.c.campaign_id == CAMPAIGN_ID,
            ),
        )).first()
    if not row:
        return None
    return {"status": "already", "granted_micro": int(row[0]), "remaining_micro": int(row[1])}


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
    global _campaign_ready
    try:
        existing = await _existing_grant(account_id)
        if existing:
            return existing
        digest = address_hash(remote_address)
        if digest is None:
            return {"status": "address_unknown", **nothing}
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
