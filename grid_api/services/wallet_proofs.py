# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify personal_sign proofs for EOAs and deployed EIP-1271 wallets."""

from __future__ import annotations

import os

import httpx

_EIP1271_SELECTOR = bytes.fromhex("1626ba7e")
_RPC_TIMEOUT_SECONDS = 8.0


async def _rpc(method: str, params: list) -> object:
    rpc_url = os.getenv("GRID_BASE_RPC", "https://mainnet.base.org").strip()
    if not rpc_url:
        raise RuntimeError("Base RPC is not configured")
    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT_SECONDS) as client:
        response = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Base RPC rejected wallet proof")
    return payload.get("result")


async def verify_personal_signature(
    *,
    message: str,
    signature: str,
    address: str,
) -> bool:
    """Verify an EIP-191 signature, falling back to EIP-1271 on Base."""
    from eth_account import Account
    from eth_account.messages import defunct_hash_message, encode_defunct

    expected = address.strip().lower()
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message),
            signature=signature,
        ).lower()
    except Exception:
        recovered = ""
    if recovered == expected:
        return True

    try:
        raw_signature = bytes.fromhex(signature.removeprefix("0x"))
    except ValueError:
        return False
    try:
        code = await _rpc("eth_getCode", [expected, "latest"])
    except (httpx.HTTPError, RuntimeError, ValueError):
        return False
    if not isinstance(code, str) or code in {"0x", "0x0", "0x00"}:
        return False

    from eth_abi import encode

    message_hash = bytes(defunct_hash_message(text=message))
    call_data = "0x" + (
        _EIP1271_SELECTOR + encode(["bytes32", "bytes"], [message_hash, raw_signature])
    ).hex()
    try:
        result = await _rpc(
            "eth_call",
            [{"to": expected, "data": call_data}, "latest"],
        )
    except (httpx.HTTPError, RuntimeError, ValueError):
        return False
    return isinstance(result, str) and result.lower().startswith("0x1626ba7e")
