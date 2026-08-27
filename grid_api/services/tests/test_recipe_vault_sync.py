# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import gzip
import hashlib
import json
import threading
from dataclasses import replace

import pytest
from pydantic import SecretStr
from web3 import Web3

from grid_api._abi import decompress_workflow
from grid_api.config import GridSettings
from grid_api.services import recipe_vault_sync, recipes

DIAMOND = "0x" + "1" * 40
FACET = "0x" + "2" * 40
OTHER_FACET = "0x" + "3" * 40
CREATOR = "0x" + "4" * 40
RUNTIME = b"reviewed-recipe-vault-runtime"
RUNTIME_HASH = Web3.keccak(RUNTIME).hex()
BLOCK_HASH = "0x" + "5" * 64
VERIFIER = "recipe-vault-v1-30c1d6d"
REVIEWED_RUNTIME_HASH = (
    "0x4c585d77c8dfd729bb6a93e6d2451c6a39584c7f10eb4a66691e7a70a7c88c60"
)


def _workflow(name: str, *, model: str = "test-model") -> tuple[dict, bytes, str]:
    workflow = {
        "_grid": {
            "clamps": {},
            "deterministic": False,
            "engine": "comfyui",
            "enums": {},
            "jobType": "image",
            "modelName": model,
            "name": name,
            "requiredModels": [model],
            "vars": {"prompt": "1.inputs.text"},
        },
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }
    raw = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode()
    return workflow, raw, "0x" + hashlib.sha256(raw).hexdigest()


def _record(
    recipe_id: int,
    name: str,
    *,
    public: bool = True,
    workflow_data: bytes | None = None,
    root: str | None = None,
) -> recipe_vault_sync.OnchainRecipeRecord:
    _, canonical, canonical_root = _workflow(name)
    return recipe_vault_sync.OnchainRecipeRecord(
        recipe_id=recipe_id,
        recipe_root=root or canonical_root,
        workflow_data=workflow_data if workflow_data is not None else canonical,
        creator=CREATOR,
        can_create_nfts=False,
        is_public=public,
        compression=0,
        created_at=1_700_000_000 + recipe_id,
        name=name,
        description="test",
    )


def _snapshot(*records, block=123_456):
    return recipe_vault_sync.FinalizedRecipeSnapshot(
        chain_id=8453,
        diamond_address=DIAMOND,
        facet_address=FACET,
        facet_runtime_hash=RUNTIME_HASH,
        finalized_block=block,
        finalized_block_hash=BLOCK_HASH,
        finalized_block_timestamp=1_700_000_000,
        records=tuple(records),
    )


class _Call:
    def __init__(self, value, calls, name):
        self.value = value
        self.calls = calls
        self.name = name

    def call(self, *, block_identifier):
        self.calls.append((self.name, block_identifier))
        return self.value


class _Functions:
    def __init__(self, *, routes, records, calls):
        self.routes = routes
        self.records = records
        self.calls = calls

    def moduleAddress(self, selector):
        key = "0x" + bytes(selector).hex()
        return _Call(self.routes[key], self.calls, f"route:{key}")

    def getTotalRecipes(self):
        return _Call(len(self.records), self.calls, "count")

    def getRecipe(self, recipe_id):
        record = self.records[recipe_id - 1]
        value = (
            record.recipe_id,
            bytes.fromhex(record.recipe_root[2:]),
            record.workflow_data,
            record.creator,
            record.can_create_nfts,
            record.is_public,
            record.compression,
            record.created_at,
            record.name,
            record.description,
        )
        return _Call(value, self.calls, f"recipe:{recipe_id}")


class _Contract:
    def __init__(self, functions):
        self.functions = functions


class _Eth:
    def __init__(self, *, routes, records, runtime=RUNTIME, chain_id=8453):
        self.chain_id = chain_id
        self.calls = []
        self.runtime = runtime
        self.contract_value = _Contract(
            _Functions(routes=routes, records=records, calls=self.calls),
        )

    def get_block(self, tag):
        number = 123_456 if tag == "finalized" else int(tag)
        return {
            "number": number,
            "hash": bytes.fromhex(BLOCK_HASH[2:]),
            "timestamp": 1_700_000_000,
        }

    def get_code(self, address, *, block_identifier):
        self.calls.append((f"code:{str(address).lower()}", block_identifier))
        return self.runtime

    def contract(self, *, address, abi):
        assert str(address).lower() == DIAMOND
        assert abi
        return self.contract_value


class _Web3:
    def __init__(self, eth, *, connected=True):
        self.eth = eth
        self.connected = connected

    def is_connected(self):
        return self.connected


def _routes(*, override=None):
    result = {selector: FACET for selector in recipe_vault_sync.RECIPE_VAULT_SELECTORS}
    if override:
        result[recipe_vault_sync.RECIPE_VAULT_SELECTORS[0]] = override
    return result


def _read_with(eth, **overrides):
    kwargs = {
        "rpc_url": "https://rpc.invalid",
        "expected_chain_id": 8453,
        "diamond_address": DIAMOND,
        "expected_facet_runtime_hash": RUNTIME_HASH,
        "max_records": 10,
        "max_workflow_bytes": 64 * 1024,
        "rpc_timeout_seconds": 10,
        "max_finalized_age_seconds": 1800,
        "web3_factory": lambda _url, _timeout: _Web3(eth),
        "now_unix": lambda: 1_700_000_100,
    }
    kwargs.update(overrides)
    return recipe_vault_sync.read_finalized_recipe_snapshot(**kwargs)


def test_verifier_label_pins_merged_contract_runtime():
    assert recipe_vault_sync.REVIEWED_RECIPE_VAULT_RUNTIMES == {
        VERIFIER: REVIEWED_RUNTIME_HASH,
    }
    assert recipe_vault_sync.reviewed_runtime_hash(f" {VERIFIER} ") == REVIEWED_RUNTIME_HASH
    assert recipe_vault_sync.reviewed_runtime_hash("unreviewed") is None


def test_reads_every_selector_and_record_at_one_finalized_block():
    record = _record(1, "chain-recipe")
    eth = _Eth(routes=_routes(), records=[record])

    snapshot = _read_with(eth)

    assert snapshot.records == (record,)
    assert snapshot.finalized_block == 123_456
    assert len([name for name, _ in eth.calls if name.startswith("route:")]) == 10
    assert {block for _, block in eth.calls} == {123_456}


@pytest.mark.parametrize(
    ("eth", "match"),
    [
        (_Eth(routes=_routes(override=OTHER_FACET), records=[]), "do not route to one facet"),
        (_Eth(routes=_routes(), records=[], runtime=b"unreviewed"), "runtime hash is not reviewed"),
        (_Eth(routes=_routes(), records=[], chain_id=1), "chain id does not match"),
    ],
)
def test_rejects_wrong_route_runtime_or_chain(eth, match):
    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match=match):
        _read_with(eth)


def test_rejects_record_count_and_workflow_size_above_bounds():
    records = [_record(1, "one"), _record(2, "two")]
    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match="record count"):
        _read_with(_Eth(routes=_routes(), records=records), max_records=1)

    oversized = replace(records[0], workflow_data=b"x" * 101)
    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match="workflow size"):
        _read_with(
            _Eth(routes=_routes(), records=[oversized]),
            max_workflow_bytes=100,
        )


def test_rejects_stale_or_future_finalized_block():
    eth = _Eth(routes=_routes(), records=[])
    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match="freshness"):
        _read_with(eth, now_unix=lambda: 1_700_001_801)
    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match="freshness"):
        _read_with(eth, now_unix=lambda: 1_699_999_939)


def test_quorum_uses_common_finalized_block_and_rejects_disagreement():
    expected = _snapshot(_record(1, "one"), block=123_455)
    calls = []

    def reader(**kwargs):
        requested = kwargs.get("finalized_block")
        calls.append((kwargs["rpc_url"], requested))
        if requested is not None:
            return replace(expected, finalized_block=requested)
        tip = 123_456 if kwargs["rpc_url"] == "https://primary.invalid" else 123_455
        return replace(expected, finalized_block=tip)

    result = recipe_vault_sync.read_quorum_recipe_snapshot(
        rpc_url="https://primary.invalid",
        confirmation_rpc_url="https://confirmation.invalid",
        expected_chain_id=8453,
        diamond_address=DIAMOND,
        expected_facet_runtime_hash=RUNTIME_HASH,
        max_records=10,
        max_workflow_bytes=64 * 1024,
        rpc_timeout_seconds=10,
        max_finalized_age_seconds=1800,
        single_reader=reader,
    )
    assert result.finalized_block == 123_455
    assert calls == [
        ("https://primary.invalid", None),
        ("https://confirmation.invalid", None),
        ("https://primary.invalid", 123_455),
        ("https://confirmation.invalid", 123_455),
    ]

    def disagreeing_reader(**kwargs):
        snapshot = expected
        if kwargs["rpc_url"] == "https://confirmation.invalid":
            snapshot = replace(snapshot, records=(_record(1, "different"),))
        return snapshot

    with pytest.raises(recipe_vault_sync.RecipeVaultSyncError, match="disagree"):
        recipe_vault_sync.read_quorum_recipe_snapshot(
            rpc_url="https://primary.invalid",
            confirmation_rpc_url="https://confirmation.invalid",
            expected_chain_id=8453,
            diamond_address=DIAMOND,
            expected_facet_runtime_hash=RUNTIME_HASH,
            max_records=10,
            max_workflow_bytes=64 * 1024,
            rpc_timeout_seconds=10,
            max_finalized_age_seconds=1800,
            single_reader=disagreeing_reader,
        )


@pytest.fixture(autouse=True)
def _clean_recipe_sources():
    with recipes._CACHE_LOCK:
        for cache in (
            recipes._BY_ROOT,
            recipes._BY_ID,
            recipes._BY_NAME,
            recipes._BY_MODEL,
            recipes._LOCAL_RECIPES,
            recipes._ONCHAIN_RECIPES,
        ):
            cache.clear()
        recipes._ONCHAIN_MASKED_ROOTS.clear()
        recipes._ONCHAIN_MASKED_NAMES.clear()
        recipes._ONCHAIN_SYNCED_AT = None
        recipes._ONCHAIN_FINALIZED_BLOCK = None
        recipes._ONCHAIN_FINALIZED_BLOCK_HASH = None
    yield
    with recipes._CACHE_LOCK:
        for cache in (
            recipes._BY_ROOT,
            recipes._BY_ID,
            recipes._BY_NAME,
            recipes._BY_MODEL,
            recipes._LOCAL_RECIPES,
            recipes._ONCHAIN_RECIPES,
        ):
            cache.clear()
        recipes._ONCHAIN_MASKED_ROOTS.clear()
        recipes._ONCHAIN_MASKED_NAMES.clear()
        recipes._ONCHAIN_SYNCED_AT = None
        recipes._ONCHAIN_FINALIZED_BLOCK = None
        recipes._ONCHAIN_FINALIZED_BLOCK_HASH = None


def _settings():
    return GridSettings(
        base_rpc_url=SecretStr("https://primary.invalid"),
        recipevault_sync_enabled=True,
        recipevault_address=DIAMOND,
        recipevault_confirmation_rpc_url=SecretStr("https://confirmation.invalid"),
        recipevault_verifier_version=VERIFIER,
    )


def test_public_content_is_verified_and_private_content_is_not_parsed():
    public = _record(1, "public")
    private = _record(2, "private", public=False, workflow_data=b"not-json", root="0x" + "9" * 64)

    staged = recipes._stage_onchain_recipes(_snapshot(public, private))

    assert list(staged) == [public.recipe_root]
    assert staged[public.recipe_root].recipe_id == 1


@pytest.mark.parametrize(
    ("record", "match"),
    [
        (_record(1, "bad-root", root="0x" + "9" * 64), "root does not commit"),
        (_record(1, "compressed"), "uncompressed"),
        (_record(1, "noncanonical"), "Core-canonical"),
    ],
)
def test_rejects_untrusted_public_content(record, match):
    if record.name == "compressed":
        record = replace(record, compression=1)
    elif record.name == "noncanonical":
        workflow, _, _ = _workflow(record.name)
        record = replace(
            record,
            workflow_data=json.dumps(workflow, indent=2).encode(),
            recipe_root="0x" + hashlib.sha256(json.dumps(workflow, indent=2).encode()).hexdigest(),
        )
    with pytest.raises(ValueError, match=match):
        recipes._stage_onchain_recipes(_snapshot(record))


def test_rejects_duplicate_keys_and_nonfinite_numbers():
    for raw, match in (
        (b'{"_grid":{},"_grid":{}}', "duplicate"),
        (b'{"_grid":{"value":NaN}}', "non-finite"),
    ):
        record = _record(
            1,
            "invalid-json",
            workflow_data=raw,
            root="0x" + hashlib.sha256(raw).hexdigest(),
        )
        with pytest.raises(ValueError, match=match):
            recipes._stage_onchain_recipes(_snapshot(record))


def test_malformed_later_record_does_not_partially_replace_cache(monkeypatch):
    old = _record(1, "old")
    monkeypatch.setattr(
        recipe_vault_sync,
        "read_quorum_recipe_snapshot",
        lambda **_kwargs: _snapshot(old),
    )
    assert recipes._sync_from_recipevault_blocking(_settings()) == 1
    assert recipes.get_recipe("old").recipe_id == 1

    good_new = _record(2, "good-new")
    bad_later = _record(3, "bad-later", root="0x" + "8" * 64)
    monkeypatch.setattr(
        recipe_vault_sync,
        "read_quorum_recipe_snapshot",
        lambda **_kwargs: _snapshot(good_new, bad_later),
    )
    with pytest.raises(ValueError, match="root does not commit"):
        recipes._sync_from_recipevault_blocking(_settings())

    assert recipes.get_recipe("old").recipe_id == 1
    assert recipes.get_recipe("good-new") is None


def test_private_revocation_evicts_chain_entry_and_masks_local_copy(monkeypatch, tmp_path):
    local_workflow, _, _ = _workflow("shared")
    recipe_path = tmp_path / "shared.json"
    recipe_path.write_text(json.dumps(local_workflow))
    recipes.load_local_recipes(str(tmp_path))
    local = recipes.get_recipe("shared")
    assert local is not None and local.recipe_id is None

    chain = _record(1, "shared")
    current = _snapshot(chain)
    monkeypatch.setattr(
        recipe_vault_sync,
        "read_quorum_recipe_snapshot",
        lambda **_kwargs: current,
    )
    assert recipes._sync_from_recipevault_blocking(_settings()) == 1
    assert recipes.get_recipe("shared").recipe_id == 1

    current = _snapshot(replace(chain, is_public=False))
    assert recipes._sync_from_recipevault_blocking(_settings()) == 0
    assert recipes.get_recipe("shared") is None


def test_stale_chain_layer_expires_to_local_without_request_path_rpc(monkeypatch, tmp_path):
    local_workflow, _, _ = _workflow("shared")
    (tmp_path / "shared.json").write_text(json.dumps(local_workflow))
    unrelated_workflow, _, _ = _workflow("unrelated")
    (tmp_path / "unrelated.json").write_text(json.dumps(unrelated_workflow))
    recipes.load_local_recipes(str(tmp_path))

    monkeypatch.setattr(recipes.time, "monotonic", lambda: 100.0)
    chain_record = _record(1, "shared")
    snapshot = _snapshot(chain_record)
    recipes._install_onchain_snapshot(recipes._stage_onchain_recipes(snapshot), snapshot)
    assert recipes.get_recipe("shared").recipe_id == 1

    settings = _settings()
    settings.recipevault_max_stale_seconds = 60
    monkeypatch.setattr(recipes, "get_settings", lambda: settings)
    monkeypatch.setattr(recipes.time, "monotonic", lambda: 161.0)
    expired = recipes.get_recipe("shared")

    assert expired is None
    assert recipes.get_recipe("unrelated") is not None
    assert recipes._ONCHAIN_RECIPES == {}
    assert chain_record.recipe_root in recipes._ONCHAIN_MASKED_ROOTS


def test_install_rejects_finalized_height_rollback():
    newer = _snapshot(_record(1, "newer"), block=200)
    recipes._install_onchain_snapshot(recipes._stage_onchain_recipes(newer), newer)

    older = _snapshot(_record(1, "older"), block=199)
    with pytest.raises(ValueError, match="roll back"):
        recipes._install_onchain_snapshot(recipes._stage_onchain_recipes(older), older)

    assert recipes.get_recipe("newer") is not None
    assert recipes.get_recipe("older") is None

    conflicting_hash = replace(newer, finalized_block_hash="0x" + "6" * 64)
    with pytest.raises(ValueError, match="hash changed"):
        recipes._install_onchain_snapshot(
            recipes._stage_onchain_recipes(conflicting_hash),
            conflicting_hash,
        )


def test_cache_reads_and_stale_expiry_are_serialized(monkeypatch):
    settings = _settings()
    settings.recipevault_max_stale_seconds = 60
    monkeypatch.setattr(recipes, "get_settings", lambda: settings)
    monkeypatch.setattr(recipes.time, "monotonic", lambda: 161.0)
    chain = _record(1, "chain")
    with recipes._CACHE_LOCK:
        recipes._ONCHAIN_RECIPES[chain.recipe_root] = recipes._recipe_from_workflow(
            chain.recipe_root,
            chain.name,
            _workflow(chain.name)[0],
            recipe_id=chain.recipe_id,
        )
        recipes._ONCHAIN_MASKED_ROOTS.add(chain.recipe_root)
        recipes._ONCHAIN_MASKED_NAMES.add(chain.name.lower())
        recipes._ONCHAIN_SYNCED_AT = 100.0
        recipes._ONCHAIN_FINALIZED_BLOCK = 123_456
        recipes._ONCHAIN_FINALIZED_BLOCK_HASH = BLOCK_HASH
        recipes._rebuild_source_cache_locked()

    errors: list[BaseException] = []

    def read_repeatedly():
        try:
            for _ in range(100):
                recipes.list_recipes()
                recipes.get_recipe("chain")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=read_repeatedly) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert recipes._ONCHAIN_RECIPES == {}


@pytest.mark.asyncio
async def test_sync_is_explicitly_disabled_and_never_falls_back_to_grid_address(monkeypatch):
    settings = _settings()
    settings.recipevault_sync_enabled = False
    monkeypatch.setattr(recipes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        recipes,
        "_sync_from_recipevault_blocking",
        lambda _settings: pytest.fail("disabled sync attempted a chain read"),
    )

    assert await recipes.sync_from_recipevault() == 0


def test_decompression_is_bounded_and_rejects_unsafe_codecs():
    compressed = gzip.compress(b"x" * 10_000)
    with pytest.raises(ValueError, match="size limit"):
        decompress_workflow(compressed, 1, max_output_bytes=100)
    assert decompress_workflow(gzip.compress(b"ok"), 1, max_output_bytes=100) == b"ok"
    with pytest.raises(ValueError, match="brotli"):
        decompress_workflow(b"x", 2, max_output_bytes=100)
    with pytest.raises(ValueError, match="unknown compression"):
        decompress_workflow(b"x", 99, max_output_bytes=100)
