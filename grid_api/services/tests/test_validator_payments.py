# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from eth_abi import decode, encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from web3 import Web3
from web3.exceptions import TransactionNotFound

from grid_api.services import validator_compensation_recipients as recipients
from grid_api.services.settlement import payouts
from grid_api.services.settlement import validator_payments as pay
from grid_api.services.tests.test_validator_compensation_postgres import PG, close_time, comp, create, members, report, tables
from grid_api.services.tests.test_validator_compensation_postgres import db as db
from grid_api.services.tests.test_validator_compensation_recipients import signed

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not PG.startswith("postgresql"), reason="disposable PostgreSQL required")]


class FakeEth:
    def __init__(self):
        self.chain_id = 8453
        self.pending_nonce = self.mined_nonce = 10
        self.base_fee = 1
        self.finalized = 100
        self.block_hash = HexBytes("0x" + "ab" * 32)
        self.auto_mine = True
        self.lose_response = False
        self.mutate_receipt = lambda receipt: receipt
        self.receipts = {}
        self.broadcasts = []

    def get_transaction_count(self, address, block="latest"):
        assert Web3.is_checksum_address(address)
        return self.pending_nonce if block == "pending" else self.mined_nonce

    def get_block(self, block):
        return {"number": self.finalized if block == "finalized" else 100, "hash": self.block_hash, "baseFeePerGas": self.base_fee}

    def get_balance(self, address):
        return 10**18

    def estimate_gas(self, request):
        return 50_000

    def get_transaction_receipt(self, tx_hash):
        if tx_hash not in self.receipts:
            raise TransactionNotFound("fixture transaction not mined")
        return self.receipts[tx_hash]

    def send_raw_transaction(self, raw):
        raw = bytes(raw)
        self.broadcasts.append(raw)
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        tx_hash = Web3.to_hex(Web3.keccak(raw))
        recipient, amount = decode(["address", "uint256"], tx["data"][4:])
        if self.auto_mine:
            self.receipts[tx_hash] = self.mutate_receipt(
                {
                    "transactionHash": HexBytes(tx_hash),
                    "status": 1,
                    "blockNumber": 100,
                    "blockHash": self.block_hash,
                    "logs": [
                        {
                            "address": Web3.to_hex(tx["to"]),
                            "removed": False,
                            "topics": [
                                pay.TRANSFER_TOPIC,
                                HexBytes(encode(["address"], [Account.recover_transaction(raw)])),
                                HexBytes(encode(["address"], [recipient])),
                            ],
                            "data": HexBytes(encode(["uint256"], [amount])),
                        },
                    ],
                },
            )
            self.mined_nonce = max(self.mined_nonce, tx["nonce"] + 1)
        if self.lose_response:
            raise ConnectionError("fixture response loss")
        return HexBytes(tx_hash)


async def setup(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    for node in group:
        await report(db, node)
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    for i, node in enumerate(group):
        wallet = Account.create()
        prepared = await recipients.prepare_recipient("pilot-fixture", f"opg_fixture{i:08d}", wallet.address.lower())
        request = signed(prepared["consent"], node[2], wallet)
        preview = await recipients.bind_recipient(request)
        await recipients.bind_recipient(request, apply=True, expected_digest=preview["digest"])
    signer = Account.create()
    eth = FakeEth()
    monkeypatch.setattr(pay, "_context", lambda plan: (SimpleNamespace(eth=eth), signer))
    monkeypatch.setattr(pay, "get_settings", lambda: SimpleNamespace(validator_compensation_send_enabled=True))

    async def screen(plan):
        return None

    monkeypatch.setattr(pay, "_screen", screen)
    request = {
        "campaign_id": "pilot-fixture",
        "operator_group_id": "opg_fixture00000000",
        "sender": signer.address.lower(),
        "max_fee_per_gas": "100000000",
        "max_priority_fee_per_gas": "1000000",
        "approved_until": (comp._now() + timedelta(hours=1)).isoformat(),
        "approval_ref": "approval:payment-fixture",
    }
    return request, eth, signer, group


async def rows(db):
    async with db() as session:
        return (await session.execute(sa.select(tables.validator_compensation_payments))).mappings().all()


async def execute(request):
    preview = await pay.preview_payment(request)
    return await pay.send_payment(request, expected_digest=preview["digest"])


async def test_preview_never_loads_key_or_rpc_and_send_gate_is_dark(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    monkeypatch.setattr(pay, "_context", lambda plan: pytest.fail("no RPC or signing"))
    monkeypatch.setattr(pay, "get_settings", lambda: SimpleNamespace(validator_compensation_send_enabled=False))
    preview = await pay.preview_payment(request)
    assert preview["dry_run"] and preview["plan"]["amount_atomic"] == str((10**18 + 1) // 3)
    with pytest.raises(comp.CompensationError, match="disabled"):
        await pay.send_payment(request, expected_digest=preview["digest"])
    assert not await rows(db) and not eth.broadcasts


async def test_exact_finalized_transfer_and_repeat_do_not_double_pay(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    result = await execute(request)
    assert result["status"] == "sent", result
    assert await execute(request) == result
    stored = (await rows(db))[0]
    assert stored["receipt"]["amount_atomic"] == str((10**18 + 1) // 3)
    assert len(eth.broadcasts) == 1 and stored["raw_transaction"] == eth.broadcasts[0]
    async with db() as session:
        for table in (tables.payouts, tables.payout_legs, tables.ledger, tables.credit_ledger):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0


@pytest.mark.parametrize(
    "fault", ["wrong_sender", "wrong_recipient", "wrong_token", "wrong_amount", "no_transfer", "removed", "reverted", "wrong_hash"],
)
async def test_receipt_without_exact_transfer_cannot_mark_sent(db, monkeypatch, fault):
    request, eth, _, _ = await setup(db, monkeypatch)

    def corrupt(receipt):
        log = receipt["logs"][0]
        if fault == "wrong_sender":
            log["topics"][1] = HexBytes("0x" + "00" * 32)
        elif fault == "wrong_recipient":
            log["topics"][2] = HexBytes("0x" + "00" * 32)
        elif fault == "wrong_token":
            log["address"] = "0x" + "12" * 20
        elif fault == "wrong_amount":
            log["data"] = HexBytes(encode(["uint256"], [1]))
        elif fault == "no_transfer":
            receipt["logs"] = []
        elif fault == "removed":
            log["removed"] = True
        elif fault == "reverted":
            receipt["status"] = 0
        else:
            receipt["transactionHash"] = HexBytes("0x" + "ff" * 32)
        return receipt

    eth.mutate_receipt = corrupt
    assert (await execute(request))["status"] == "manual_review"
    assert (await execute(request))["status"] == "manual_review"
    assert len(await rows(db)) == 1 and len(eth.broadcasts) == 1


async def test_finality_wait_and_reorg_are_pending_not_paid(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    eth.finalized = 99
    assert (await execute(request))["reason"] == "awaiting_finality"
    original = eth.block_hash
    eth.block_hash = HexBytes("0x" + "cd" * 32)
    assert (await execute(request))["reason"] == "receipt_reorged"
    eth.block_hash, eth.finalized = original, 100
    assert (await execute(request))["status"] == "sent"
    assert len(eth.broadcasts) == 1


async def test_rpc_accepts_then_loses_response_reconciles_without_resend(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    eth.lose_response = True
    assert (await execute(request))["reason"] == "broadcast_unknown"
    assert (await execute(request))["status"] == "sent"
    assert len(eth.broadcasts) == 1


async def test_missing_transaction_rebroadcasts_identical_bytes_and_nonce(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    eth.auto_mine = False
    assert (await execute(request))["status"] == "pending"
    original = (await rows(db))[0]
    eth.pending_nonce = 100  # an RPC cannot push a retry to a different nonce
    monkeypatch.setattr(comp, "_now", lambda: comp._time(request["approved_until"]) + timedelta(days=1))
    eth.auto_mine = True
    assert (await execute(request))["status"] == "sent"
    assert eth.broadcasts == [original["raw_transaction"], original["raw_transaction"]]


async def test_consumed_nonce_without_receipt_holds_instead_of_sending_again(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    eth.auto_mine = False
    await execute(request)
    eth.mined_nonce = 11
    assert (await execute(request))["reason"] == "nonce_consumed_unproven"
    assert (await execute(request))["status"] == "manual_review"
    assert len(eth.broadcasts) == 1


async def test_crash_after_commit_before_broadcast_keeps_original_transaction(db, monkeypatch):
    class ProcessStopped(BaseException):
        pass

    request, eth, _, _ = await setup(db, monkeypatch)
    original = pay._receipt
    monkeypatch.setattr(pay, "_receipt", lambda *args: (_ for _ in ()).throw(ProcessStopped()))
    with pytest.raises(ProcessStopped):
        await execute(request)
    stored = (await rows(db))[0]
    assert not eth.broadcasts
    monkeypatch.setattr(pay, "_receipt", original)
    assert (await execute(request))["status"] == "sent"
    assert eth.broadcasts == [stored["raw_transaction"]]


async def test_actual_pg_contention_allocates_one_nonce_across_twenty_runners(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    preview = await pay.preview_payment(request)
    async with db() as blocker:
        await blocker.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": payouts._PAYOUT_LOCK_KEY})
        pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
        tasks = [asyncio.create_task(pay.send_payment(request, expected_digest=preview["digest"])) for _ in range(20)]
        try:
            async with asyncio.timeout(15):
                async with db() as observer:
                    while True:
                        waiting = await observer.scalar(
                            sa.text("SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"), {"pid": pid},
                        )
                        if waiting >= 2:
                            break
                        assert not any(task.done() for task in tasks)
                        await observer.rollback()
                        await asyncio.sleep(0.01)
        finally:
            await blocker.rollback()
            try:
                async with asyncio.timeout(30):
                    results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(item, dict) and item["status"] == "sent" for item in results), results
    assert len(await rows(db)) == 1
    assert len(set(eth.broadcasts)) == 1


async def test_two_allocations_and_worker_rails_share_nonce_space(db, monkeypatch):
    request, eth, _, group = await setup(db, monkeypatch)
    async with db() as session:
        await session.execute(
            sa.insert(tables.payouts).values(period_id="fixture", account_id=group[0][1], aipg_amount=1, nonce=40, status="pending"),
        )
        await session.execute(
            sa.insert(tables.payout_legs).values(
                period_id="fixture", account_id=group[0][1], asset="AIPG", amount=1, nonce=42, status="pending",
            ),
        )
        await session.commit()
    second = {**request, "operator_group_id": "opg_fixture00000001"}
    a, b = await asyncio.gather(execute(request), execute(second))
    assert a["status"] == b["status"] == "sent"
    assert sorted(row["nonce"] for row in await rows(db)) == [43, 44]
    assert await payouts._max_assigned_nonce() == 44


async def test_wrong_digest_does_not_load_signer(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    monkeypatch.setattr(pay, "_context", lambda plan: pytest.fail("must reject before signing"))
    with pytest.raises(comp.CompensationError, match="digest"):
        await pay.send_payment(request, expected_digest="a" * 64)
    assert not await rows(db) and not eth.broadcasts


async def test_sanctions_hold_never_broadcasts(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)

    async def blocked(plan):
        return "blocked_sanctions"

    monkeypatch.setattr(pay, "_screen", blocked)
    with pytest.raises(comp.CompensationError, match="screening"):
        await execute(request)
    assert not await rows(db) and not eth.broadcasts


@pytest.mark.parametrize("field", ["nonce", "raw_transaction", "sender", "plan_hash", "tx_hash"])
async def test_corrupt_stored_transaction_is_never_broadcast(db, monkeypatch, field):
    request, eth, _, _ = await setup(db, monkeypatch)
    eth.auto_mine = False
    await execute(request)
    stored = (await rows(db))[0]
    changes = {
        "nonce": stored["nonce"] + 1,
        "raw_transaction": b"invalid",
        "sender": "0x" + "12" * 20,
        "plan_hash": "a" * 64,
        "tx_hash": "0x" + "bc" * 32,
    }
    async with db() as session:
        await session.execute(sa.update(pay.payments).values(**{field: changes[field]}))
        await session.commit()
    with pytest.raises(comp.CompensationError):
        await execute(request)
    assert len(eth.broadcasts) == 1


async def test_commit_failure_rolls_back_nonce_and_never_broadcasts(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)

    async def fail_commit(self):
        raise ConnectionError("fixture commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(ConnectionError):
            await execute(request)
    assert not await rows(db) and not eth.broadcasts
    assert (await execute(request))["status"] == "sent"
    assert (await rows(db))[0]["nonce"] == 10


async def test_approval_expiring_during_lock_wait_cannot_sign(db, monkeypatch):
    request, eth, _, _ = await setup(db, monkeypatch)
    original = comp._lock

    async def delay(session):
        await original(session)
        monkeypatch.setattr(comp, "_now", lambda: comp._time(request["approved_until"]))

    monkeypatch.setattr(comp, "_lock", delay)
    with pytest.raises(comp.CompensationError, match="approval"):
        await execute(request)
    assert not await rows(db) and not eth.broadcasts


@pytest.mark.parametrize("fault", ["fee", "balance", "estimate"])
async def test_preflight_failure_does_not_bind_nonce(db, monkeypatch, fault):
    request, eth, _, _ = await setup(db, monkeypatch)
    if fault == "fee":
        eth.base_fee = int(request["max_fee_per_gas"])
    elif fault == "balance":
        eth.get_balance = lambda address: 0
    else:
        eth.estimate_gas = lambda tx: pay.GAS_LIMIT + 1
    with pytest.raises(comp.CompensationError):
        await execute(request)
    assert not await rows(db) and not eth.broadcasts


@pytest.mark.parametrize("fault", [None, "chain", "signer", "token", "code", "decimals"])
async def test_context_verifies_chain_token_and_signer(db, monkeypatch, fault):
    context = pay._context
    request, _, signer, _ = await setup(db, monkeypatch)
    plan = (await pay.preview_payment(request))["plan"]
    token = SimpleNamespace(
        address=plan["token_address"],
        functions=SimpleNamespace(decimals=lambda: SimpleNamespace(call=lambda: 6 if fault == "decimals" else 18)),
    )
    eth = SimpleNamespace(
        chain_id=1 if fault == "chain" else 8453,
        get_code=lambda address: b"" if fault == "code" else b"fixture",
        contract=lambda **kw: token,
    )
    options = []

    class Client:
        to_checksum_address = staticmethod(Web3.to_checksum_address)

        @staticmethod
        def HTTPProvider(url, **kwargs):
            options.append(kwargs)
            return object()

        def __new__(cls, provider):
            return SimpleNamespace(eth=eth)

    monkeypatch.setattr(pay, "Web3", Client)
    monkeypatch.setattr(payouts, "BASE_RPC_URL", "https://rpc.invalid")
    monkeypatch.setattr(payouts, "TREASURY_PK", Account.create().key if fault == "signer" else signer.key)
    monkeypatch.setattr(payouts, "AIPG_TOKEN_ADDRESS", "0x" + "12" * 20 if fault == "token" else plan["token_address"])
    if fault:
        with pytest.raises(comp.CompensationError, match="configured chain"):
            context(plan)
    else:
        _, account = context(plan)
        assert account.address == signer.address
    assert options == [{"request_kwargs": {"timeout": 15}}]


async def test_shared_worker_lock_failure_is_not_treated_as_acquired():
    class BrokenSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, *args):
            raise ConnectionError("fixture lock unavailable")

    with pytest.raises(ConnectionError):
        await payouts._try_payout_lock(BrokenSession())


async def test_private_cli_previews_read_only_then_sends_without_schema_bootstrap(db, monkeypatch, tmp_path):
    from grid_api import database
    from scripts import pay_validator_allocation as cli
    from scripts.preview_validator_compensation import write_private

    request, eth, _, _ = await setup(db, monkeypatch)
    source = tmp_path / "request.json"
    write_private(source, request)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(async_database_url=PG))
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    modes = []

    def engine(url, **options):
        modes.append(options["connect_args"]["server_settings"]["default_transaction_read_only"])
        return create_async_engine(url, execution_options={"schema_translate_map": {None: namespace}}, **options)

    monkeypatch.setattr(cli, "create_async_engine", engine)
    monkeypatch.setattr(database, "init_database", lambda *args: pytest.fail("must not bootstrap schema"))
    args = SimpleNamespace(input=source, send=False, expect_digest=None)
    preview = await cli.run(args)
    assert preview["status"] == "preview" and not await rows(db)
    args.send, args.expect_digest = True, preview["digest"]
    assert (await cli.run(args))["status"] == "sent"
    assert len(eth.broadcasts) == 1 and modes == ["on", "off"]


async def test_real_pg_migration_roundtrip_and_refusal_to_erase_payment(db, monkeypatch):
    request, _, _, _ = await setup(db, monkeypatch)
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0038_validator_compensation_payments.py"
    spec = importlib.util.spec_from_file_location("payment_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = db.kw["bind"]
    namespace = engine.get_execution_options()["schema_translate_map"][None]

    def roundtrip(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        pay.payments.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            columns = sa.inspect(connection).get_columns(pay.payments.name, schema=namespace)
            assert {column["name"]: column["nullable"] for column in columns} == {
                column.name: column.nullable for column in pay.payments.c
            }
            assert len(sa.inspect(connection).get_check_constraints(pay.payments.name, schema=namespace)) == 4
            revision.downgrade()
            revision.upgrade()

    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)
    assert (await execute(request))["status"] == "sent"

    def refuse(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="refuse to discard"):
                revision.downgrade()

    async with engine.begin() as connection:
        await connection.run_sync(refuse)
    assert (await rows(db))[0]["status"] == "sent"
