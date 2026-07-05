# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USDC-on-Base deposit → credit rail (the demand-side funding front door).

A user sends USDC to the grid treasury on Base, then submits the tx hash; we
verify the transfer on-chain and credit their prepaid balance 1:1 (USDC has 6
decimals, so its base-unit value IS micro-USD — no oracle, no conversion). The
credits service (credit(), idempotent on `ref`) is the only thing that moves the
balance, so a tx can never double-credit.

Config-gated and DORMANT until deployed with a treasury address (mirrors the
charging-dark / probe-dark pattern): with GRID_USDC_TREASURY unset the claim
endpoint returns 503, so this ships safely before the treasury exists.

Security posture:
- The deposit is bound to the authenticated account's SIWE wallet (tx `from`
  must equal it) so nobody can claim someone else's transfer.
- Confirmation-gated (GRID_DEPOSIT_CONFIRMATIONS) so a reorg can't un-mine a
  credited tx.
- Idempotent on the tx hash via credits.credit(ref=...).
"""

import logging
import os

import httpx
from fastapi import HTTPException

from . import credits

logger = logging.getLogger("grid_api.deposits")

DEPOSITS_ENABLED = os.getenv("GRID_DEPOSITS_ENABLED", "0").lower() in ("1", "true", "yes", "on")
# Where users send USDC. Unset → rail is dormant (503).
TREASURY = os.getenv("GRID_USDC_TREASURY", "").strip().lower()
BASE_RPC = os.getenv("GRID_BASE_RPC", "https://mainnet.base.org").strip()
# USDC on Base (6 decimals) — the canonical Circle contract. Overridable for testnet.
USDC = os.getenv("GRID_USDC_CONTRACT", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913").strip().lower()
CONFIRMATIONS = int(os.getenv("GRID_DEPOSIT_CONFIRMATIONS", "3") or 3)
# Native-ETH deposits credit at the Chainlink ETH/USD price at claim time (a
# deposit-time oracle; the request path stays oracle-free). Recipient defaults to
# the same treasury as USDC. Unset treasury → the ETH claim endpoint 503s too.
ETH_TREASURY = (os.getenv("GRID_ETH_TREASURY", "") or TREASURY).strip().lower()

# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def is_configured() -> bool:
    return DEPOSITS_ENABLED and bool(TREASURY)


async def _rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"rpc {method}: {body['error']}")
        return body.get("result")


def _addr_from_topic(topic: str) -> str:
    # 32-byte-padded address → 0x + last 20 bytes.
    return ("0x" + topic[-40:]).lower()


async def verify_and_credit(tx_hash: str, account: dict) -> dict:
    """Verify a USDC-to-treasury transfer and credit the account. Idempotent."""
    if not is_configured():
        raise HTTPException(503, detail="USDC deposits are not enabled on this grid yet.")
    tx_hash = (tx_hash or "").strip().lower()
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        raise HTTPException(400, detail="tx_hash must be a 0x-prefixed 32-byte hash.")

    try:
        receipt = await _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as e:
        logger.warning("deposit rpc failed for %s: %s", tx_hash, e)
        raise HTTPException(502, detail="Could not reach Base to verify the transaction.")
    if not receipt:
        raise HTTPException(400, detail="Transaction not found or not yet mined.")
    if receipt.get("status") not in ("0x1", 1):
        raise HTTPException(400, detail="Transaction failed on-chain.")

    # Confirmation gate — a reorg must not un-mine a credited deposit.
    try:
        latest = int(await _rpc("eth_blockNumber", []), 16)
        confs = latest - int(receipt["blockNumber"], 16)
    except Exception:
        confs = 0
    if confs < CONFIRMATIONS:
        raise HTTPException(425, detail=f"Only {confs} confirmations; need {CONFIRMATIONS}. Retry shortly.")

    # Find the USDC Transfer whose recipient is our treasury.
    value_micro = 0
    sender = ""
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() == USDC
            and len(topics) >= 3
            and topics[0].lower() == _TRANSFER_TOPIC
            and _addr_from_topic(topics[2]) == TREASURY
        ):
            sender = _addr_from_topic(topics[1])
            value_micro = int(log.get("data", "0x0"), 16)  # USDC base units == micro-USD
            break
    if value_micro <= 0:
        raise HTTPException(400, detail="No USDC transfer to the grid treasury found in this transaction.")

    # Bind the deposit to the account's wallet so a transfer can't be claimed by
    # someone else. (Account wallet is set at SIWE login.)
    acct_wallet = (account.get("wallet") or "").lower()
    if not acct_wallet:
        raise HTTPException(403, detail="Link a wallet (sign in with your wallet) before claiming deposits.")
    if acct_wallet != sender:
        raise HTTPException(403, detail="This deposit was sent from a different wallet than your account's.")

    applied = await credits.credit(
        account["account_id"], value_micro, reason="usdc_deposit", ref=f"usdc:{tx_hash}"
    )
    balance = await credits.get_balance(account["account_id"])
    return {
        "credited": bool(applied),
        "already_claimed": not applied,
        "amount_usd": round(value_micro / 1_000_000, 6),
        "balance_usd": round(balance / 1_000_000, 6),
        "from": sender,
        "tx_hash": tx_hash,
    }


def eth_is_configured() -> bool:
    return DEPOSITS_ENABLED and bool(ETH_TREASURY)


async def verify_and_credit_eth(tx_hash: str, account: dict) -> dict:
    """Verify a native-ETH transfer to the treasury and credit the account in USD.

    Same posture as the USDC path (SIWE-wallet bound, confirmation-gated, idempotent
    on the tx hash) — the only difference is the amount is priced ETH→USD via the
    Chainlink ETH/USD feed on Base at claim time (deposit-time oracle only)."""
    if not eth_is_configured():
        raise HTTPException(503, detail="ETH deposits are not enabled on this grid yet.")
    tx_hash = (tx_hash or "").strip().lower()
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        raise HTTPException(400, detail="tx_hash must be a 0x-prefixed 32-byte hash.")

    try:
        tx = await _rpc("eth_getTransactionByHash", [tx_hash])
        receipt = await _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as e:
        logger.warning("eth deposit rpc failed for %s: %s", tx_hash, e)
        raise HTTPException(502, detail="Could not reach Base to verify the transaction.")
    if not tx or not receipt:
        raise HTTPException(400, detail="Transaction not found or not yet mined.")
    if receipt.get("status") not in ("0x1", 1):
        raise HTTPException(400, detail="Transaction failed on-chain.")

    # A plain ETH send: tx.to is the treasury and it carried value. (Value forwarded
    # via a contract's internal call is intentionally NOT credited — direct sends only.)
    if (tx.get("to") or "").lower() != ETH_TREASURY:
        raise HTTPException(400, detail="This transaction did not send ETH to the grid treasury.")
    value_wei = int(tx.get("value", "0x0") or "0x0", 16)
    if value_wei <= 0:
        raise HTTPException(400, detail="No ETH value in this transaction.")

    # Confirmation gate — a reorg must not un-mine a credited deposit.
    try:
        latest = int(await _rpc("eth_blockNumber", []), 16)
        confs = latest - int(receipt["blockNumber"], 16)
    except Exception:
        confs = 0
    if confs < CONFIRMATIONS:
        raise HTTPException(425, detail=f"Only {confs} confirmations; need {CONFIRMATIONS}. Retry shortly.")

    # Bind to the account's own wallet (tx sender).
    sender = (tx.get("from") or "").lower()
    acct_wallet = (account.get("wallet") or "").lower()
    if not acct_wallet:
        raise HTTPException(403, detail="Link a wallet (sign in with your wallet) before claiming deposits.")
    if acct_wallet != sender:
        raise HTTPException(403, detail="This deposit was sent from a different wallet than your account's.")

    # Price ETH→USD at claim time (Chainlink on Base). Never guess a price.
    from . import holdings
    try:
        px_micro = await holdings.eth_usd_micro()  # micro-USD per 1 ETH
    except Exception as e:
        logger.warning("eth/usd price read failed for deposit %s: %s", tx_hash, e)
        raise HTTPException(502, detail="Could not read the ETH/USD price feed; retry shortly.")
    value_micro = value_wei * px_micro // (10 ** 18)
    if value_micro <= 0:
        raise HTTPException(400, detail="ETH amount too small to credit at the current price.")

    applied = await credits.credit(
        account["account_id"], value_micro, reason="eth_deposit", ref=f"eth:{tx_hash}"
    )
    balance = await credits.get_balance(account["account_id"])
    return {
        "credited": bool(applied),
        "already_claimed": not applied,
        "amount_usd": round(value_micro / 1_000_000, 6),
        "eth": round(value_wei / 1e18, 8),
        "eth_usd": round(px_micro / 1_000_000, 2),
        "balance_usd": round(balance / 1_000_000, 6),
        "from": sender,
        "tx_hash": tx_hash,
    }
