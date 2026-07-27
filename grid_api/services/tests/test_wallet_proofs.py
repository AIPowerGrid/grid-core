# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from eth_abi import decode
from eth_account import Account
from eth_account.messages import defunct_hash_message, encode_defunct

from grid_api.services import wallet_proofs


@pytest.mark.asyncio
async def test_eoa_signature_never_calls_rpc(monkeypatch):
    wallet = Account.create()
    message = "Sign in to AI Power Grid."
    signature = Account.sign_message(
        encode_defunct(text=message),
        wallet.key,
    ).signature.hex()

    async def no_rpc(*_args):
        raise AssertionError("EOA verification should not call RPC")

    monkeypatch.setattr(wallet_proofs, "_rpc", no_rpc)
    assert await wallet_proofs.verify_personal_signature(
        message=message,
        signature=signature,
        address=wallet.address,
    )


@pytest.mark.asyncio
async def test_deployed_eip1271_signature_uses_base_contract(monkeypatch):
    contract = "0x0000000000000000000000000000000000000127"
    message = "Contract wallet proof"
    signature = "0x1234"
    calls = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            data = bytes.fromhex(params[0]["data"][2:])
            assert data[:4].hex() == "1626ba7e"
            message_hash, raw_signature = decode(["bytes32", "bytes"], data[4:])
            assert message_hash == bytes(defunct_hash_message(text=message))
            assert raw_signature == bytes.fromhex("1234")
            assert params[0]["to"] == contract
            return "0x1626ba7e00000000000000000000000000000000000000000000000000000000"
        raise AssertionError(method)

    monkeypatch.setattr(wallet_proofs, "_rpc", fake_rpc)
    assert await wallet_proofs.verify_personal_signature(
        message=message,
        signature=signature,
        address=contract,
    )
    assert [method for method, _ in calls] == ["eth_getCode", "eth_call"]


@pytest.mark.asyncio
async def test_eip1271_wrong_magic_fails(monkeypatch):
    async def fake_rpc(method, _params):
        return "0x6000" if method == "eth_getCode" else "0xffffffff"

    monkeypatch.setattr(wallet_proofs, "_rpc", fake_rpc)
    assert not await wallet_proofs.verify_personal_signature(
        message="bad proof",
        signature="0x1234",
        address="0x0000000000000000000000000000000000000127",
    )
