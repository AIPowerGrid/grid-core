# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only Base treasury checks; no signer, sender, or automatic refill."""

import asyncio
import logging
import re

import httpx

from ..config import get_settings
from . import alerts

logger = logging.getLogger("grid_api.treasury_health")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})\Z")
_WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")


async def inspect(settings, client: httpx.AsyncClient) -> dict:
    """Read both balances at one Base block, rejecting incomplete RPC responses."""
    next_id = 0

    async def rpc(method, params, *, word=False):
        nonlocal next_id
        next_id += 1
        response = await client.post(
            settings.base_rpc_url.get_secret_value(),
            json={"jsonrpc": "2.0", "id": next_id, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
        if (not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                or type(body.get("id")) is not int or body["id"] != next_id
                or "error" in body):
            raise ValueError("Invalid treasury RPC envelope")
        value = body.get("result")
        pattern = _WORD if word else _QUANTITY
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("Invalid treasury RPC result")
        return int(value, 16)

    if await rpc("eth_chainId", []) != 8453:
        raise ValueError("Treasury RPC is not Base mainnet")
    block = hex(await rpc("eth_blockNumber", []))
    wallet = settings.grid_treasury_monitor_wallet
    eth = await rpc("eth_getBalance", [wallet, block])
    token = await rpc("eth_call", [{
        "to": settings.grid_treasury_monitor_token,
        "data": "0x70a08231" + wallet[2:].lower().rjust(64, "0"),
    }, block], word=True)
    return {
        "eth_wei": eth, "token_raw": token,
        "low_eth": eth < settings.grid_treasury_min_eth_wei,
        "low_token": token < settings.grid_treasury_min_token_raw,
    }


async def check_and_alert() -> None:
    settings = get_settings()
    if not settings.grid_treasury_monitor_enabled:
        return
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                health = await inspect(settings, client)
    except Exception as exc:
        # Provider exception strings can contain authenticated RPC URLs.
        logger.warning("Treasury check unavailable error_type=%s", type(exc).__name__)
        alerts.emit(
            "treasury_monitor_failed", "warning",
            "Treasury balances are unknown because the read-only Base check failed.",
            fields={"error_type": type(exc).__name__},
        )
        return
    for asset, low, balance, threshold in (
        ("eth", health["low_eth"], health["eth_wei"], settings.grid_treasury_min_eth_wei),
        ("aipg", health["low_token"], health["token_raw"], settings.grid_treasury_min_token_raw),
    ):
        if low:
            alerts.emit(
                f"treasury_low_{asset}", "warning",
                "Treasury balance is below its configured warning threshold. No automatic refill was attempted.",
                fields={"asset": asset, "balance_raw": balance, "threshold_raw": threshold},
            )
