# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import create_async_engine

from grid_api.services import validator_compensation_recipients as service
from grid_api.services.tests.test_validator_compensation_postgres import (
    PG,
    close_time,
    comp,
    create,
    members,
    report,
    tables,
)
from grid_api.services.tests.test_validator_compensation_postgres import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not PG.startswith("postgresql"), reason="disposable PostgreSQL required")]


def signed(consent, node, recipient):
    message = encode_defunct(text=service.consent_message(consent))
    return {
        "consent": consent,
        "node_signature": "0x" + Account.sign_message(message, node.key).signature.hex().removeprefix("0x"),
        "recipient_signature": "0x" + Account.sign_message(message, recipient.key).signature.hex().removeprefix("0x"),
        "approval_ref": "approval:recipient-fixture",
    }


async def setup(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    recipient = Account.create()
    prepared = await service.prepare_recipient("pilot-fixture", "opg_fixture00000000", recipient.address.lower())
    return group, recipient, signed(prepared["consent"], group[0][2], recipient)


async def count(db):
    async with db() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_recipients))


async def test_preview_apply_retry_keeps_exact_amount_and_never_pays(db, monkeypatch):
    _, _, request = await setup(db, monkeypatch)
    assert request["consent"]["amount_atomic"] == str(10**18 + 1)
    preview = await service.bind_recipient(request)
    assert preview["status"] == "preview" and await count(db) == 0
    result = await service.bind_recipient(request, apply=True, expected_digest=preview["digest"])
    assert result["status"] == "bound" and result["sendable"] is False
    monkeypatch.setattr(comp, "_now", lambda: comp._time(request["consent"]["expires_at"]) + timedelta(days=2))
    assert await service.bind_recipient(request, apply=True, expected_digest=result["digest"]) == result
    assert await count(db) == 1
    async with db() as session:
        for table in (tables.payouts, tables.payout_legs, tables.ledger, tables.credit_ledger):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_20_duplicate_racers_wait_for_real_pg_lock_and_bind_once(db, monkeypatch):
    _, _, request = await setup(db, monkeypatch)
    preview = await service.bind_recipient(request)
    async with db() as blocker:
        await comp._lock(blocker)
        tasks = [asyncio.create_task(service.bind_recipient(request, apply=True, expected_digest=preview["digest"])) for _ in range(20)]
        try:
            # Observe actual advisory-lock waiters, not just successful asyncio scheduling.
            for _ in range(200):
                waiting = await blocker.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = :hi AND objid = :lo",
                    ),
                    {"hi": comp.LOCK_KEY >> 32, "lo": comp.LOCK_KEY & 0xFFFFFFFF},
                )
                if waiting == 20:
                    break
                await asyncio.sleep(0.02)
            assert waiting == 20
        finally:
            await blocker.rollback()
            results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(item, dict) and item["status"] == "bound" for item in results), results
    assert await count(db) == 1


async def test_conflicting_recipients_cannot_overwrite(db, monkeypatch):
    group, _, request = await setup(db, monkeypatch)
    other = Account.create()
    second = signed({**request["consent"], "recipient": other.address.lower()}, group[0][2], other)
    a, b = await service.bind_recipient(request), await service.bind_recipient(second)
    results = await asyncio.gather(
        service.bind_recipient(request, apply=True, expected_digest=a["digest"]),
        service.bind_recipient(second, apply=True, expected_digest=b["digest"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, comp.CompensationError) for result in results) == 1
    assert await count(db) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("campaign_id", "another-campaign"),
        ("operator_group_id", "opg_fixture00000001"),
        ("allocation_hash", "f" * 64),
        ("contract_hash", "f" * 64),
        ("amount_atomic", "999"),
        ("validator_id", "val_" + "f" * 32),
        ("account_id", "00000000-0000-0000-0000-000000000001"),
        ("token_address", "0x" + "12" * 20),
    ],
)
async def test_even_valid_signatures_cannot_change_allocation(db, monkeypatch, field, value):
    group, recipient, request = await setup(db, monkeypatch)
    changed = signed({**request["consent"], field: value}, group[0][2], recipient)
    with pytest.raises(comp.CompensationError):
        await service.bind_recipient(changed, apply=True, expected_digest=comp._hash(changed))
    assert await count(db) == 0


@pytest.mark.parametrize("field", ["node_signature", "recipient_signature"])
async def test_both_signatures_required(db, monkeypatch, field):
    _, _, request = await setup(db, monkeypatch)
    request[field] = "0x" + "aa" * 65

    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline RPC")

    monkeypatch.setattr(service.wallet_proofs, "_rpc", unavailable)
    with pytest.raises(comp.CompensationError):
        await service.bind_recipient(request, apply=True, expected_digest=comp._hash(request))
    assert await count(db) == 0


@pytest.mark.parametrize(
    "change",
    ["expired", "future", "wrong_chain", "wrong_audience", "node_recipient", "zero_recipient", "zero_amount", "wide_expiry", "extra_field"],
)
async def test_domain_bounds_and_freshness(db, monkeypatch, change):
    _, _, request = await setup(db, monkeypatch)
    consent = request["consent"]
    if change == "expired":
        monkeypatch.setattr(comp, "_now", lambda: comp._time(consent["expires_at"]))
    elif change == "future":
        monkeypatch.setattr(comp, "_now", lambda: comp._time(consent["issued_at"]) - timedelta(seconds=1))
    elif change == "wrong_chain":
        consent["chain_id"] = 1
    elif change == "wrong_audience":
        consent["audience"] = "https://other.invalid"
    elif change == "node_recipient":
        consent["recipient"] = consent["signing_wallet"]
    elif change == "zero_recipient":
        consent["recipient"] = "0x" + "0" * 40
    elif change == "zero_amount":
        consent["amount_atomic"] = "0"
    elif change == "wide_expiry":
        consent["expires_at"] = (comp._time(consent["issued_at"]) + timedelta(days=2)).isoformat()
    else:
        consent["extra"] = "not authorized"
    with pytest.raises(comp.CompensationError):
        await service.bind_recipient(request, apply=True, expected_digest=comp._hash(request))
    assert await count(db) == 0


async def test_identity_rotation_invalidates_prepared_request(db, monkeypatch):
    group, _, request = await setup(db, monkeypatch)
    async with db() as session:
        await session.execute(
            sa.update(tables.validators)
            .where(tables.validators.c.id == group[0][0])
            .values(signing_wallet=Account.create().address.lower()),
        )
        await session.commit()
    with pytest.raises(comp.CompensationError, match="identity changed"):
        await service.bind_recipient(request, apply=True, expected_digest=comp._hash(request))
    assert await count(db) == 0


@pytest.mark.parametrize("chain", ["0x1", "0x2105"])
async def test_contract_wallet_requires_base_and_valid_eip1271(db, monkeypatch, chain):
    group, _, request = await setup(db, monkeypatch)
    recipient = Account.create()
    request = signed({**request["consent"], "recipient": recipient.address.lower()}, group[0][2], recipient)
    request["recipient_signature"] = "0x1234"
    calls = []

    async def rpc(method, params):
        calls.append(method)
        if method == "eth_chainId":
            return chain
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            assert params[0]["to"] == recipient.address.lower()
            assert params[0]["data"].startswith("0x1626ba7e")
            return "0x1626ba7e" + "0" * 56
        pytest.fail(method)

    monkeypatch.setattr(service.wallet_proofs, "_rpc", rpc)
    if chain != "0x2105":
        with pytest.raises(comp.CompensationError):
            await service.bind_recipient(request)
        assert calls == ["eth_chainId"]
    else:
        preview = await service.bind_recipient(request)
        assert calls == ["eth_chainId", "eth_getCode", "eth_call"]
        assert preview["status"] == "preview"
    assert await count(db) == 0


async def test_stale_digest_and_corrupt_stored_proof_fail_closed(db, monkeypatch):
    _, _, request = await setup(db, monkeypatch)
    with pytest.raises(comp.CompensationError, match="digest"):
        await service.bind_recipient(request, apply=True, expected_digest="0" * 64)
    preview = await service.bind_recipient(request)
    await service.bind_recipient(request, apply=True, expected_digest=preview["digest"])
    corrupt = copy.deepcopy(request)
    corrupt["approval_ref"] = "approval:changed"
    async with db() as session:
        await session.execute(sa.update(tables.validator_compensation_recipients).values(proof=corrupt))
        await session.commit()
    with pytest.raises(comp.CompensationError, match="commitment"):
        await service.bind_recipient(request)


async def test_retired_account_is_not_silently_followed_to_another_owner(db, monkeypatch):
    group, _, request = await setup(db, monkeypatch)
    async with db() as session:
        await session.execute(
            sa.insert(tables.account_aliases).values(
                source_account_id=group[0][1],
                canonical_account_id=group[1][1],
                merge_ref="fixture-retirement",
                reason="fixture",
            ),
        )
        await session.commit()
    with pytest.raises(comp.CompensationError, match="account changed"):
        await service.bind_recipient(request, apply=True, expected_digest=comp._hash(request))
    assert await count(db) == 0


async def test_database_failure_rolls_back_binding_and_retry_works(db, monkeypatch):
    _, _, request = await setup(db, monkeypatch)
    preview = await service.bind_recipient(request)
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    async with db() as session:
        await session.execute(
            sa.text(
                f'CREATE FUNCTION "{namespace}".reject_consent() RETURNS trigger LANGUAGE plpgsql '
                "AS $$ BEGIN RAISE EXCEPTION 'fixture write failure'; END $$",
            ),
        )
        await session.execute(
            sa.text(
                f'CREATE TRIGGER reject_consent AFTER INSERT ON "{namespace}".grid_validator_compensation_recipients '
                f'FOR EACH ROW EXECUTE FUNCTION "{namespace}".reject_consent()',
            ),
        )
        await session.commit()
    with pytest.raises(sa.exc.DBAPIError):
        await service.bind_recipient(request, apply=True, expected_digest=preview["digest"])
    assert await count(db) == 0
    async with db() as session:
        await session.execute(sa.text(f'DROP TRIGGER reject_consent ON "{namespace}".grid_validator_compensation_recipients'))
        await session.commit()
    result = await service.bind_recipient(request, apply=True, expected_digest=preview["digest"])
    assert result["status"] == "bound" and await count(db) == 1


async def test_expiration_during_contract_verification_is_not_committed(db, monkeypatch):
    _, _, request = await setup(db, monkeypatch)
    request["recipient_signature"] = "0x1234"

    async def rpc(method, params):
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_getCode":
            return "0x6000"
        monkeypatch.setattr(comp, "_now", lambda: comp._time(request["consent"]["expires_at"]))
        return "0x1626ba7e"

    monkeypatch.setattr(service.wallet_proofs, "_rpc", rpc)
    with pytest.raises(comp.CompensationError, match="expired during"):
        await service.bind_recipient(request, apply=True, expected_digest=comp._hash(request))
    assert await count(db) == 0


async def test_private_command_reads_real_pg_without_ddl_and_commits_only_apply(db, monkeypatch, tmp_path):
    from scripts import manage_validator_recipient as cli
    from scripts.preview_validator_compensation import write_private

    _, _, request = await setup(db, monkeypatch)
    source = tmp_path / "signed.json"
    write_private(source, request)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(async_database_url=PG))
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    modes = []

    def engine(url, **options):
        modes.append(options["connect_args"]["server_settings"]["default_transaction_read_only"])
        return create_async_engine(url, execution_options={"schema_translate_map": {None: namespace}}, **options)

    monkeypatch.setattr(cli, "create_async_engine", engine)
    args = SimpleNamespace(input=source, action="bind", apply=False, expect_digest=None)
    preview = await cli.run(args)
    assert preview["status"] == "preview" and await count(db) == 0
    args.apply, args.expect_digest = True, preview["digest"]
    result = await cli.run(args)
    assert result["status"] == "bound" and await count(db) == 1
    assert modes == ["on", "off"]
