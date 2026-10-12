# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit local-Anvil execution only; upstream fork sources are read-only."""

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from eth_abi import decode, encode
from eth_utils import keccak

from grid_api.services import video_payment_proofs as proofs
from grid_api.services import video_refund_proofs as refunds

_ZERO = "0x" + "0" * 40
_EXEC = keccak(text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)")[:4]
_EXEC_TYPES = ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"]
_READS = {"web3_clientVersion", "anvil_nodeInfo", "eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call",
          "eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_getLogs"}
_LOCAL_WRITES = {"evm_snapshot", "evm_revert", "evm_setNextBlockTimestamp", "anvil_impersonateAccount",
                 "anvil_stopImpersonatingAccount", "anvil_setBalance", "anvil_mine", "eth_sendTransaction"}


class LocalTransactionReverted(ValueError):
    """A simulated EVM revert, not transport/provider failure."""


def local_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError
        return f"http://127.0.0.1:{parsed.port}"
    except (TypeError, ValueError):
        raise ValueError("literal loopback Anvil URL required") from None


class LocalFork:
    def __init__(self, origin, safe_address):
        self.origin = local_origin(origin)
        self.safe = proofs.quotes._address(safe_address)
        self.client = httpx.AsyncClient(trust_env=False, timeout=30)
        self.identity = None
        self.calls = []

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, params):
        if method not in _READS | _LOCAL_WRITES:
            raise ValueError("unsupported local-fork method")
        response = await self.client.post(self.origin, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response.raise_for_status()
        if len(response.content) > 512 * 1024:
            raise ValueError("local fork reply bound exceeded")
        message = json.loads(response.content)
        if (type(message) is dict and set(message) == {"jsonrpc", "id", "error"}
                and message["id"] == 1 and message["jsonrpc"] == "2.0"):
            error = message["error"]
            if (method == "eth_sendTransaction" and type(error) is dict and error.get("code") == 3
                    and type(error.get("message")) is str and error["message"].startswith("execution reverted")
                    and type(error.get("data")) is str):
                proofs._hex(error["data"], maximum=8192)
                raise LocalTransactionReverted("local simulated transaction reverted")
        if set(message) != {"jsonrpc", "id", "result"} or message["id"] != 1 or message["jsonrpc"] != "2.0":
            raise ValueError("local fork RPC unavailable")
        self.calls.append(method)
        return message["result"]

    async def verify(self):
        version = await self._request("web3_clientVersion", [])
        info = await self._request("anvil_nodeInfo", [])
        fork = info.get("forkConfig", {}) if type(info) is dict else {}
        origin = urlsplit(fork.get("forkUrl", ""))
        if (type(version) is not str or not version.lower().startswith("anvil/")
                or await self._request("eth_chainId", []) != "0x2105"
                or origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.query or origin.fragment or type(fork.get("forkBlockNumber")) is not int
                or fork["forkBlockNumber"] <= 0 or info.get("environment", {}).get("chainId") != 8453):
            raise ValueError("verified local Base fork required before simulation")
        identity = {"client": version, "fork_url": fork["forkUrl"], "fork_block": fork["forkBlockNumber"]}
        if self.identity is not None and self.identity != identity:
            raise ValueError("local fork identity changed")
        self.identity = identity
        return identity

    async def rpc(self, method, params):
        if method in _LOCAL_WRITES:
            await self.verify()
        return await self._request(method, params)

    async def call(self, to, signature, args=b"", block="latest"):
        encoded = "0x" + (keccak(text=signature)[:4] + args).hex()
        return await self.rpc("eth_call", [{"to": to, "data": encoded}, block])

    async def number(self, to, signature, args=b""):
        return int(await self.call(to, signature, args), 16)

    async def balance(self, asset, account):
        return await self.number(asset, "balanceOf(address)", encode(["address"], [account]))

    async def send(self, sender, to, data):
        tx = await self.rpc("eth_sendTransaction", [{"from": sender, "to": to, "data": data, "gas": hex(1_500_000)}])
        for _ in range(100):
            receipt = await self.rpc("eth_getTransactionReceipt", [tx])
            if receipt is not None:
                if int(receipt["status"], 16) != 1:
                    raise LocalTransactionReverted("local simulated transaction reverted")
                return tx, receipt
            await asyncio.sleep(0.05)
        raise ValueError("local transaction still pending; do not send again")

    async def owners(self):
        version = decode(["string"], bytes.fromhex((await self.call(self.safe, "VERSION()"))[2:]))[0]
        threshold = await self.number(self.safe, "getThreshold()")
        owners = decode(["address[]"], bytes.fromhex((await self.call(self.safe, "getOwners()"))[2:]))[0]
        if version != "1.4.1" or not 1 <= threshold <= len(owners) <= 16:
            raise ValueError("bounded Safe 1.4.1 owner set required")
        return tuple(sorted(owners)[:threshold])

    async def unlock(self, address):
        await self.rpc("anvil_impersonateAccount", [address])
        await self.rpc("anvil_setBalance", [address, hex(10**18)])

    async def execute_safe(self, intent):
        owners = await self.owners()
        safe_hash = refunds.transaction_hash(intent)
        signatures = b""
        for owner in owners:
            await self.unlock(owner)
            data = "0x" + (keccak(text="approveHash(bytes32)")[:4] + bytes.fromhex(safe_hash[2:])).hex()
            await self.send(owner, self.safe, data)
            # Approved-hash signatures exercise Safe itself without production keys.
            signatures += bytes.fromhex(owner[2:]).rjust(32, b"\0") + b"\0" * 32 + b"\x01"
        tx = refunds.transaction(intent)
        values = [tx["to"], 0, bytes.fromhex(tx["data"][2:]), 0, 0, 0, 0, _ZERO, _ZERO, signatures]
        encoded = "0x" + (_EXEC + encode(_EXEC_TYPES, values)).hex()
        result = await self.send(owners[0], self.safe, encoded)
        return result, encoded, owners[0]

    async def seed_payer(self, asset, payer, amount):
        if type(amount) is not int or not 0 < amount <= 100_000 or await self.balance(asset, self.safe) < amount:
            raise ValueError("bounded copied fork balance required")
        nonce = await self.number(self.safe, "nonce()")
        intent = {"chain_id": 8453, "asset": asset, "safe_address": self.safe,
                  "payer": payer, "amount_micro": amount, "safe_nonce": nonce}
        await self.execute_safe(intent)
        if await self.balance(asset, payer) != amount:
            raise ValueError("local payer seeding did not match")

    async def after(self, epoch):
        latest = await self.rpc("eth_getBlockByNumber", ["latest", False])
        target = max(int(latest["timestamp"], 16) + 1, int(epoch) + 1, int(time.time()))
        await self.rpc("evm_setNextBlockTimestamp", [target])

    async def confirmations(self):
        # Anvil's safe/finalized tags trail epochs; do not fake them as latest.
        await self.rpc("anvil_mine", [hex(96), "0x1"])

    @asynccontextmanager
    async def proof_sources(self):
        """Two logical HTTPS adapters share one local fork, not independent RPCs."""
        async def handle(request):
            message = json.loads(request.content)
            if message.get("method") not in _READS - {"web3_clientVersion", "anvil_nodeInfo"}:
                raise ValueError("proof adapter is read-only")
            # This bridge forwards real Anvil evidence; it fabricates no receipts.
            value = await self.rpc(message["method"], message["params"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": copy.deepcopy(value)})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as one:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as two:
                yield (proofs.PaymentRPC("https://fork-one.example/", one), proofs.PaymentRPC("https://fork-two.example/", two))
