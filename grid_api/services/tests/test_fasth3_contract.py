# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline H3 contract checks, not GPU, license, paid-billing or deployment proof."""

import copy
import itertools
import json
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from grid_api.config import GridSettings
from grid_api.services import credits, generation_admission, pricing, recipes
from grid_api.services import fasth3_contract as h3


@pytest.fixture
def registry(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})


def candidate(**values):
    return h3.compile_candidate({"prompt": "A sunny park.", **values})


def test_startup_catalog_never_loads_or_advertises_candidate(registry):
    root = Path(__file__).resolve().parents[3]
    assert recipes.load_local_recipes(str(root / "recipes")) > 0
    assert recipes.get_recipe(h3.MODEL) is None
    assert recipes.video_contract_for_model(h3.MODEL) is None
    assert recipes.generation_modes(h3.MODEL) == []
    assert recipes.param_schema(h3.MODEL) is None
    assert h3.MODEL not in {recipe.model_name for recipe in recipes.list_recipes()}
    assert pricing.get_price(h3.MODEL) is None
    # This low-level helper returns zero for a missing rate; enforcement must
    # distinguish unpriced from free before reserving or dispatching work.
    assert pricing.quote_video(h3.MODEL, 124 / 24) == 0
    assert not pricing.is_priced_for(h3.MODEL, "video")


def test_unreviewed_schema_cannot_register_as_live_ltx(registry):
    workflow = json.loads(h3.CANDIDATE.read_text())
    with pytest.raises(ValidationError):
        recipes.register_recipe("0xunqualified-h3", h3.MODEL, workflow)
    assert recipes.list_recipes() == []


@pytest.mark.parametrize("source", [False, True])
def test_open_public_async_switch_does_not_enable_h3(source):
    settings = GridSettings(
        _env_file=None,
        media_async_enabled=True,
        media_async_admission_mode="open",
        media_async_nvfp4_enabled=True,
        media_async_source_admission_mode="open",
    )
    with pytest.raises(HTTPException) as error:
        generation_admission.require_async_video(settings, uuid4(), model=h3.MODEL, has_source=source)
    assert error.value.status_code == 503


def test_fixed_sampling_and_component_provenance():
    result = candidate(appearance_image=True, seed=0)
    graph = result["spec"]
    meta = json.loads(h3.CANDIDATE.read_text())["_grid"]
    assert result["qualification_only"] is True
    assert result["model"] == h3.MODEL
    assert graph["model"]["inputs"]["unet_name"] == "fasth3_omniref_qad_nvfp4_comfy.safetensors"
    assert graph["encoder"]["inputs"]["type"] == "minimax"
    assert graph["conditioning"]["inputs"]["ref_image_size"] == "match"
    assert graph["noise"]["inputs"]["noise_seed"] == 0
    assert graph["sampler"]["inputs"]["sampler_name"] == "res_multistep"
    assert graph["sigmas"]["inputs"]["steps"] == 8
    assert graph["sigmas"]["inputs"]["scheduler"] == "simple"
    assert graph["guider"]["class_type"] == "BasicGuider"
    assert graph["shift"]["inputs"] == {"model": ["model", 0], "shift_video": 12.0, "shift_audio": 3.0}
    assert meta["provenance"]["model_revision"] == "b1b4d67d79930007a3f73ca8e98739796ded95a0"
    assert meta["provenance"]["comfy_revision"] == "b1a247c9cf9dbfda4b2c0a0c1d50950667eeaa0c"
    assert meta["provenance"]["license_sha256"] == "59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44"
    for node_name, field in (("model", "unet_name"), ("encoder", "clip_name"), ("video_vae", "vae_name"), ("audio_vae", "vae_name")):
        filename = graph[node_name]["inputs"][field]
        assert len(meta["provenance"]["components"][filename]) == 64


@pytest.mark.parametrize(
    "appearance,opening,audio_mode",
    list(itertools.product([False, True], [False, True], ["generated", "reference", "preserve"])),
)
def test_all_explicit_source_combinations_have_exact_slots(appearance, opening, audio_mode):
    values = {"appearance_image": appearance, "opening_image": opening, "audio_mode": audio_mode}
    if not appearance and not opening and audio_mode != "generated":
        with pytest.raises(ValueError, match="Invalid FastH3"):
            candidate(**values)
        return
    result = candidate(**values)
    graph = result["spec"]
    roles = [binding["role"] for binding in result["source_bindings"]]
    assert ("appearance_image" in roles) == appearance
    assert ("opening_image" in roles) == opening
    assert ("conditioning_audio" in roles) == (audio_mode == "reference")
    assert ("soundtrack" in roles) == (audio_mode == "preserve")
    for binding in result["source_bindings"]:
        node, inputs, field = binding["path"].split(".")
        assert isinstance(graph[node][inputs][field], str)
    for node in graph.values():
        for value in node["inputs"].values():
            if isinstance(value, list):
                assert value[0] in graph
    conditioning = graph["conditioning"]["inputs"]
    assert ("ref_images.ref_image_0" in conditioning) == appearance
    assert ("ref_audios.ref_audio_0" in conditioning) == (audio_mode == "reference")
    assert ("guide" in graph) == (opening or audio_mode == "preserve")
    if opening:
        assert graph["guide"]["inputs"]["image"] == ["opening", 0]
    if audio_mode == "preserve":
        assert graph["guide"]["inputs"]["audio"] == ["soundtrack", 0]
        assert graph["video"]["inputs"]["audio"] == ["soundtrack", 0]
        assert "decode_audio" not in graph
    else:
        assert graph["video"]["inputs"]["audio"] == ["decode_audio", 0]


@pytest.mark.parametrize("frames", range(123, 364))
def test_exact_h3_grid_never_rounds_ltx_counts_or_underquotes(frames):
    if 124 <= frames <= 362 and frames % 17 == 5:
        result = candidate(frames=frames)
        execution = result["execution"]
        assert result["spec"]["conditioning"]["inputs"]["length"] == frames
        assert Fraction(execution["duration_numerator"], execution["duration_denominator"]) == Fraction(frames, 24)
        for rate in (1, 1999, 10000, 20000):  # Synthetic rates, not approved prices.
            amount = h3.quote_units(frames, rate)
            exact = Fraction(frames * rate, 24)
            assert amount - 1 < exact <= amount
    else:
        with pytest.raises(ValueError):
            candidate(frames=frames)
        with pytest.raises(ValueError):
            h3.quote_units(frames, 10000)


@pytest.mark.parametrize("size", [(1344, 768), (768, 1344), (768, 768)])
def test_qualification_canvases_are_exact_not_auto_fit(size):
    result = candidate(width=size[0], height=size[1])
    assert result["execution"]["width"] == size[0]
    assert result["spec"]["conditioning"]["inputs"]["height"] == size[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"frames": True},
        {"frames": "124"},
        {"frames": 125},
        {"frames": 625},
        {"seconds": 5.0},
        {"seconds": float("nan")},
        {"seconds": float("inf")},
        {"width": True},
        {"width": 1344.0},
        {"width": 512},
        {"height": 512},
        {"seed": True},
        {"seed": -1},
        {"seed": 2**53},
        {"seed": "29"},
        {"steps": 7},
        {"steps": 8.0},
        {"cfg": True},
        {"cfg": 2.0},
        {"fps": 24.0},
        {"audio": 1},
        {"audio": False},
        {"sampler": "euler"},
        {"scheduler": "normal"},
        {"precision": "bf16"},
        {"reference_video": "x.mp4"},
        {"appearance_image": "x.png"},
        {"opening_image": 1},
        {"audio_mode": "audio"},
        {"prompt": " "},
        {"prompt": "x" * 8001},
        {"prompt": "\u00e9" * 4001},
        {"prompt": "<Picture 1>"},
        {"prompt": "<Audio 1>"},
        {"prompt": "<Video 1>"},
        {"prompt": "<Picture 2>", "appearance_image": True},
        {"prompt": "<Audio 1>", "appearance_image": True, "audio_mode": "preserve"},
    ],
)
def test_invalid_inputs_reject_without_reflecting_private_values(changes):
    with pytest.raises(ValueError) as error:
        candidate(**changes)
    assert str(error.value) == "Invalid FastH3 qualification inputs"


@pytest.mark.parametrize("rate", [True, 0, -1, 1.0, "10000", None])
def test_quote_rate_must_be_a_positive_server_integer(rate):
    with pytest.raises(ValueError):
        h3.quote_units(124, rate)


@pytest.mark.asyncio
async def test_unpriced_h3_cannot_reach_credit_writes(monkeypatch):
    from grid_api import database

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unpriced candidate must not open a database transaction")

    monkeypatch.setattr(credits, "charging_enabled_for", lambda *_args: True)
    monkeypatch.setattr(credits, "_economic_alert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(database, "new_session", forbidden)
    result = await credits.authorize_media(uuid4(), h3.MODEL, "video", 1, 124 / 24, uuid4())
    assert result["ok"] is False
    assert result["status"] == "unpriced" and result["reserved"] == 0


def test_changed_contract_cannot_bypass_matrix_by_model_copy():
    contract = h3.FastH3Contract.model_validate(json.loads(h3.CANDIDATE.read_text())["_grid"]["videoContract"])
    invalid = contract.model_copy(update={"max_frames": 625})
    with pytest.raises(ValueError, match="Invalid FastH3"):
        h3.normalize(invalid, {"prompt": "Park.", "frames": 124})
    smaller = contract.model_copy(update={"max_frames": 124, "canvases": ["1344x768"]})
    with pytest.raises(ValueError):
        h3.normalize(smaller, {"prompt": "Park.", "frames": 141})
    with pytest.raises(ValueError):
        h3.normalize(smaller, {"prompt": "Park.", "width": 768, "height": 768})


def test_repeated_compilation_does_not_mutate_template_or_previous_result():
    before = h3.CANDIDATE.read_bytes()
    first = candidate(appearance_image=True, audio_mode="reference")
    frozen = copy.deepcopy(first)
    second = candidate(opening_image=True, audio_mode="preserve")
    assert first == frozen
    assert first["spec"] != second["spec"]
    assert h3.CANDIDATE.read_bytes() == before
    assert candidate()["source_bindings"] == []
