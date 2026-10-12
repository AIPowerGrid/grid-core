# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in real Safe/USDC execution in a local fork, never a Base transaction.

Impersonation is confined to a verified loopback Anvil instance; signing keys
are freshly generated only for the ephemeral payer. No production key is used.
Two logical proof transports observe one fork, not independent provider trust.
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from grid_api import database
from grid_api.services import credits, video_prepaid, video_refunds
from grid_api.services import video_payment_attempts as attempts
from grid_api.services import video_payment_proofs as rpc
from grid_api.services.tests import test_video_prepaid as support
from grid_api.services.tests.video_fork_support import LocalFork, LocalTransactionReverted, local_origin
from grid_api.v2.schema import credit_ledger, ledger, metadata
from grid_api.v2.schema import payment_receipt_claims as claims

db = support.db
admission = support.admission
registry = support.registry
synthetic_dependencies = support.synthetic_dependencies
source_store = support.source_store


@pytest.mark.parametrize("origin", ["https://rpc.example", "http://localhost:8545", "http://192.0.2.1:8545",
    "http://127.0.0.1:8545/?key=x", "http://name:secret@127.0.0.1:8545", "http://127.0.0.1:8545/path"])
def test_fork_write_origin_requires_literal_localhost_before_any_client(origin):
    with pytest.raises(ValueError):
        local_origin(origin)


@pytest.mark.asyncio
@pytest.mark.parametrize("error, reverted", [
    ({"code": 3, "message": "execution reverted", "data": "0x"}, True),
    ({"code": -32000, "message": "provider unavailable"}, False),
    ({"code": 3, "message": "provider unavailable", "data": "0x"}, False),
    ({"code": 3, "message": "execution reverted", "data": "invalid"}, False),
])
async def test_fork_provider_failure_is_not_replay_rejection(error, reverted):
    fork = LocalFork("http://127.0.0.1:18549", "0x" + "1" * 40)
    await fork.close()
    async def handle(_request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})
    fork.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError) as caught:
            await fork._request("eth_sendTransaction", [])
        assert isinstance(caught.value, LocalTransactionReverted) is reverted
    finally:
        await fork.close()


@pytest.mark.asyncio
async def test_real_fork_authorization_failed_job_refund_and_replays(db, admission, record_property):
    if os.environ.get("VIDEO_PAYMENT_FORK_TEST") != "1":
        pytest.skip("explicit owned local Anvil fork required; no public-chain execution")
    if db[1] != "postgres":
        pytest.skip("fork financial proof requires real PostgreSQL")
    origin, safe = os.environ.get("VIDEO_PAYMENT_FORK_URL", ""), os.environ.get("VIDEO_PAYMENT_FORK_SAFE", "")
    fork = LocalFork(origin, safe)
    snapshot = None
    try:
        identity = await fork.verify()
        record_property("fork_block", identity["fork_block"])
        record_property("anvil_client", identity["client"])
        snapshot = await fork.rpc("evm_snapshot", [])
        payer = Account.create()
        asset = support.attempt_tests.quotes.USDC["eip155:8453"]
        for name, address in (("safe", safe), ("usdc", asset)):
            code = await fork.rpc("eth_getCode", [address, "latest"])
            record_property(f"{name}_runtime_keccak", "0x" + keccak(bytes.fromhex(code[2:])).hex())
        await fork.seed_payer(asset, payer.address.lower(), 100_000)
        starting_payer = await fork.balance(asset, payer.address.lower())
        starting_safe = await fork.balance(asset, safe)
        starting_nonce = await fork.number(safe, "nonce()")
        owner = await support.attempt_tests.quote_tests.owner(0)
        inputs = await support.attempt_tests.videos.validate_video(
            support.attempt_tests.plan_tests.request("generated"), {}, async_submission=True)
        plan = support.attempt_tests.video_plans.compile_video(inputs)
        key = str(uuid4())
        prepared = support.attempt_tests.quotes.prepare(account_id=owner, idempotency_key=key,
            request_hash=support.attempt_tests.quote_tests.INTENT, plan=plan, max_cost_micro=100_000,
            server_max_micro=100_000, network="eip155:8453", pay_to=safe)
        quote, _new = await support.attempt_tests.quotes.persist(prepared, idempotency_key=key, **support.attempt_tests.quote_tests.LIMITS)
        until = datetime.now(UTC) + timedelta(minutes=4)
        await support.attempt_tests.staging.stage(owner, quote["id"], plan, admission_until=until)
        payload = support.attempt_tests.proof_tests.binding_tests.sign(quote, payer)
        original, winner = await attempts.begin(owner, quote["id"], payload, expected_payer=payer.address,
            confirmation_tag="safe", min_confirmations=12, admission_until=until)
        assert winner and original["status"] == "settling"
        # Actual EIP-3009 signature/calldata, not a fabricated receipt.
        typed = support.attempt_tests.proof_tests.binding_tests.bindings.signing_request(quote, payer=payer.address)["typed_data"]
        signed = payer.sign_message(encode_typed_data(full_message=typed))
        auth = typed["message"]
        data = "0x" + (keccak(text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)")[:4]
            + encode(["address", "address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"],
                [auth["from"], auth["to"], int(auth["value"]), int(auth["validAfter"]), int(auth["validBefore"]),
                 bytes.fromhex(auth["nonce"][2:]), signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")])).hex()
        relayer = (await fork.owners())[0]
        await fork.unlock(relayer)
        await fork.after(quote["snapshot"]["created"])
        before_send = await fork.rpc("eth_getBlockByNumber", ["latest", False])
        search_start = int(before_send["number"], 16) + 1
        transaction, _receipt = await fork.send(relayer, asset, data)
        await fork.confirmations()
        assert await fork.balance(asset, payer.address.lower()) == starting_payer - quote["amount_micro"]
        nonce_args = encode(["address", "bytes32"], [payer.address, bytes.fromhex(auth["nonce"][2:])])
        assert await fork.number(asset, "authorizationState(address,bytes32)", nonce_args) == 1
        with pytest.raises(LocalTransactionReverted):
            await fork.send(relayer, asset, data)
        assert await fork.balance(asset, payer.address.lower()) == starting_payer - quote["amount_micro"]
        async with fork.proof_sources() as sources:
            # Discarded sender reply: recover original transaction by exact nonce.
            latest = await fork.rpc("eth_getBlockByNumber", ["safe", False])
            end = int(latest["number"], 16)
            observed = await attempts.recover_lost_transaction(owner, quote["id"], sources=sources,
                from_block=search_start, to_block=end)
            assert observed["status"] == "proven" and observed["transaction_hash"] == transaction
        payment = await video_prepaid.admit_proven(owner, quote["id"], allow_execution=True, admission_until=until)
        assert payment["admitted_at"] is not None
        await credits.release_job(payment["job_id"], only_if_unstarted_media=True)
        obligation = await video_prepaid.lookup(owner, quote["id"])
        assert obligation["refund_due_at"] is not None
        policy = {"safe_nonce": starting_nonce, "confirmation_tag": "safe", "min_confirmations": 12}
        refund = await video_refunds.prepare(owner, quote["id"], **policy)
        async with fork.proof_sources() as sources:
            with pytest.raises(rpc.VideoPaymentProofError):
                await video_refunds.reconcile(owner, quote["id"], transaction_hash=transaction, sources=sources)
        await fork.after(refund["intent"]["prepared"])
        (outbound, _receipt), encoded, sender = await fork.execute_safe(refund["intent"])
        await fork.confirmations()
        async with fork.proof_sources() as sources:
            closed = await video_refunds.reconcile(owner, quote["id"], transaction_hash=outbound, sources=sources)
            assert closed["status"] == "refunded"
            assert await video_refunds.reconcile(owner, quote["id"], transaction_hash=outbound, sources=sources) == closed
        assert await fork.balance(asset, payer.address.lower()) == starting_payer
        assert await fork.balance(asset, safe) == starting_safe
        assert await fork.number(safe, "nonce()") == starting_nonce + 1
        # Reusing the exact Safe signatures cannot transfer a second refund.
        with pytest.raises(LocalTransactionReverted):
            await fork.send(sender, safe, encoded)
        assert await fork.balance(asset, payer.address.lower()) == starting_payer
        assert await fork.number(safe, "nonce()") == starting_nonce + 1
        assert await fork.number(asset, "authorizationState(address,bytes32)", nonce_args) == 1
        assert await attempts.begin(owner, quote["id"], payload, expected_payer=payer.address,
            confirmation_tag="safe", min_confirmations=12, admission_until=until) == (observed, False)
        assert await video_prepaid.lookup(owner, quote["id"]) == obligation
        assert await video_refunds.prepare(owner, quote["id"], **policy) == closed
        assert await credits.get_balance(owner) == 0
        async with await database.new_session() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(claims)) == 2
            for table in (credit_ledger, ledger):
                assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables["grid_video_refunds"])) == 1
        record_property("simulated_paid_micro", quote["amount_micro"])
        record_property("local_only", True)
        record_property("payment_and_refund_replays_reverted", True)
        record_property("credit_and_reward_ledger_rows", 0)
    finally:
        if snapshot is not None:
            assert await fork.rpc("evm_revert", [snapshot]), "retain fork for inspection; no new payment"
        await fork.close()
