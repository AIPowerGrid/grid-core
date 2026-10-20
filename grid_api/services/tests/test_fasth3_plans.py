# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Frozen candidate staging against existing storage mechanics, not admission."""

import dataclasses
import hashlib
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from grid_api.services import credits, fasth3_sources, job_queue, media_jobs, quota, recipes, storage
from grid_api.services import fasth3_plans as plans
from grid_api.services import media_source_retention as retention
from grid_api.services.tests.test_fasth3_sources import inline, png, wav

ENDPOINT = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
BUCKET = "synthetic-private-sources"
MODES = [
    (a, o, m) for a in (False, True) for o in (False, True) for m in ("generated", "reference", "preserve") if m == "generated" or a or o
]


def forbidden(*_args, **_kwargs):
    pytest.fail("candidate staging must not authorize, dispatch, delete or use real storage")


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", ENDPOINT)
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "synthetic-public-outputs")
    preflight = Mock(return_value=BUCKET)
    monkeypatch.setattr(storage, "private_source_bucket", preflight)
    for owner, name in [
        (storage, "_client"),
        (storage, "upload_private_source"),
        (storage, "upload_source"),
        (credits, "authorize_media"),
        (job_queue, "submit_job"),
        (quota, "check_and_consume"),
        (media_jobs, "reserve"),
        (retention, "delete"),
        (retention, "sweep"),
        (recipes, "resolve_for_model"),
    ]:
        monkeypatch.setattr(owner, name, forbidden)
    return preflight


def candidate(appearance=True, opening=True, mode="preserve", **changes):
    values = {
        "prompt": "Private synthetic scene.",
        "seed": 0,
        "appearance_image": appearance,
        "opening_image": opening,
        "audio_mode": mode,
        **changes,
    }
    bundle = {}
    if appearance:
        bundle["appearance_image"] = inline(png())
    if opening:
        bundle["opening_image"] = inline(png((1344, 768)))
    if mode != "generated":
        bundle["soundtrack" if mode == "preserve" else "conditioning_audio"] = inline(wav(248_000 if mode == "preserve" else 96_000))
    return plans.compile_candidate(values, bundle)


def mock_uploads(monkeypatch, callback=None):
    uploaded = []

    def upload(raw, ext):
        uploaded.append((raw, ext))
        if callback:
            callback()
        return f"{ENDPOINT}/{BUCKET}/source/{len(uploaded):032x}.{ext}?signature=synthetic"

    monkeypatch.setattr(storage, "upload_private_source", upload)
    return uploaded


@pytest.mark.parametrize("appearance,opening,mode", MODES)
@pytest.mark.asyncio
async def test_all_roles_use_shared_frozen_staging_and_only_core_private_urls(boundaries, monkeypatch, appearance, opening, mode):
    plan = candidate(appearance, opening, mode)
    boundaries.assert_not_called()
    assert isinstance(plan, plans.video_plans.FrozenVideoPlan)
    assert plan.sha256 == hashlib.sha256(plan.snapshot_json.encode("ascii")).hexdigest()
    assert plan.snapshot["schema"] == plans.SCHEMA
    assert plan.payload["qualification_only"] is True
    assert "recipe_root" not in plan.payload
    assert "Private synthetic scene." not in repr(plan) and "Private synthetic scene." not in repr(plan.inputs)
    assert "data:" not in plan.snapshot_json
    assert plan.route_models == [plans.MODEL]
    saved = plan.snapshot_json
    graph = plan.payload["recipe_spec"]
    uploads = mock_uploads(monkeypatch)
    # Staging must consume the frozen graph, not current recipes or decoders.
    monkeypatch.setattr(fasth3_sources, "prepare_candidate", forbidden)
    view = plan.payload
    view["recipe_spec"].clear()
    view["execution"]["seed"] = 99
    staged = await plan.stage()
    assert staged["recipe_spec"] == graph and staged["seed"] == staged["execution"]["seed"] == 0
    assert plan.snapshot_json == saved
    assert len(uploads) == len(plan.inputs.sources)
    for observed, expected, uploaded in zip(staged["source_media"], plan.inputs.sources, uploads):
        assert {key: value for key, value in observed.items() if key != "url"} == expected.commitment
        assert uploaded == (expected.data, expected.ext)
        assert observed["url"].startswith(f"{ENDPOINT}/{BUCKET}/source/")
    record = retention.capture_urls([source["url"] for source in staged["source_media"]])
    assert (record is not None) == bool(plan.inputs.sources)
    if record:
        assert "signature" not in json.dumps(record) and len(record["keys"]) == len(uploads)
        retention.validate(record)
    else:
        boundaries.assert_not_called()


@pytest.mark.parametrize("mutation", ["private", "seed-bool", "seeds", "metadata", "data", "graph"])
@pytest.mark.asyncio
async def test_changed_private_inputs_reject_before_even_private_preflight(boundaries, mutation):
    plan = candidate()
    if mutation == "private":
        plan = dataclasses.replace(plan, inputs=dataclasses.replace(plan.inputs, private_images=False))
    elif mutation == "seed-bool":
        payload = plan.inputs.payload
        payload["seed"] = False
        plan = dataclasses.replace(plan, inputs=dataclasses.replace(plan.inputs, payload_json=json.dumps(payload)))
    elif mutation == "seeds":
        plan = dataclasses.replace(plan, inputs=dataclasses.replace(plan.inputs, seeds=(False,)))
    elif mutation == "metadata":
        plan.inputs.sources[0].metadata["width"] = float(plan.inputs.sources[0].metadata["width"])
    elif mutation == "data":
        first = dataclasses.replace(plan.inputs.sources[0], data=b"changed")
        plan = dataclasses.replace(plan, inputs=dataclasses.replace(plan.inputs, sources=(first, *plan.inputs.sources[1:])))
    else:
        plan = dataclasses.replace(plan, snapshot_json=plan.snapshot_json.replace('"noise_seed":0', '"noise_seed":99'))
    with pytest.raises(HTTPException) as error:
        await plan.stage()
    assert error.value.status_code == 422
    boundaries.assert_not_called()


@pytest.mark.asyncio
async def test_inflight_mutation_does_not_change_copied_source_metadata(monkeypatch):
    plan = candidate()
    mock_uploads(monkeypatch, lambda: plan.inputs.sources[-1].metadata.update(samples=1))
    staged = await plan.stage()
    assert staged["source_media"][-1]["samples"] == 248_000
    assert plan.snapshot["inputs"]["sources"][-1]["samples"] == 248_000


@pytest.mark.parametrize(
    "url",
    [
        f"{ENDPOINT}/synthetic-public-outputs/source/" + "a" * 32 + ".png?signature=synthetic",
        f"{ENDPOINT}/{BUCKET}/video/" + "a" * 32 + ".png?signature=synthetic",
        f"{ENDPOINT}/{BUCKET}/source/" + "a" * 32 + ".png",
        "https://other.invalid/private.png?signature=synthetic",
        "https://video-plan.invalid/appearance_image",
    ],
)
@pytest.mark.asyncio
async def test_foreign_public_unsigned_or_placeholder_locations_never_become_a_staged_plan(monkeypatch, url):
    plan = candidate(True, False, "generated")
    monkeypatch.setattr(storage, "upload_private_source", lambda *_args: url)
    with pytest.raises(HTTPException) as error:
        await plan.stage()
    assert error.value.status_code == 503 and url not in error.value.detail


@pytest.mark.asyncio
async def test_candidate_staging_cannot_grant_a_recipe_root_or_live_journal_authority(monkeypatch):
    # 209 lies on both H3 and LTX grids: the absence of governance must still reject.
    plan = candidate(False, False, "generated", frames=209)
    payload = await plan.stage()
    with pytest.raises(media_jobs.MediaJobError, match="governed recipe commitment"):
        media_jobs.prepare(
            account_id=uuid4(),
            idempotency_key=str(uuid4()),
            request_hash="a" * 64,
            model=plans.MODEL,
            payload=payload,
            route_models=plan.route_models,
            max_cost_micro=1000,
        )


@pytest.mark.asyncio
async def test_three_h3_sources_do_not_masquerade_as_live_ingredients_retention(monkeypatch):
    plan = candidate()
    mock_uploads(monkeypatch)
    payload = await plan.stage()
    with pytest.raises(ValueError, match="Ingredients"):
        retention.capture(payload)


def test_dispatch_bound_rejects_before_staging_or_upload(boundaries, monkeypatch):
    monkeypatch.setattr(plans, "MAX_PAYLOAD_BYTES", 1)
    with pytest.raises(HTTPException) as error:
        candidate()
    assert error.value.status_code == 413
    boundaries.assert_not_called()


@pytest.mark.asyncio
async def test_staged_commitment_types_cannot_change_without_changing_numeric_values(monkeypatch):
    plan = candidate(True, False, "generated")
    sources = plan.payload["source_media"]
    sources[0]["url"] = f"{ENDPOINT}/{BUCKET}/source/" + "a" * 32 + ".png?signature=synthetic"
    sources[0]["bytes"] = float(sources[0]["bytes"])
    monkeypatch.setattr(plans.video_plans, "stage_inputs", AsyncMock(return_value={"source_media": sources}))
    with pytest.raises(HTTPException) as error:
        await plan.stage()
    assert error.value.status_code == 422
