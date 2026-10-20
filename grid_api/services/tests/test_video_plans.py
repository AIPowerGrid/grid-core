# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic input planning/staging; no live GPU, storage or payment proof."""

import base64
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image

from grid_api.routers import videos
from grid_api.services import (
    job_queue,
    media,
    quota,
    recipes,
    storage,
    video_contract,
    video_plans,
)
from grid_api.services.tests.test_ltx25_source_contract import registry as registry
from grid_api.services.tests.test_video_sources import mp4 as mp4
from grid_api.services.tests.test_video_sources import wav

MODEL = "LTX-2.5 Distilled BF16"


def image():
    stream = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def forbidden(*_args, **_kwargs):
    pytest.fail("planning must not perform storage, quota, money or dispatch I/O")


@pytest.fixture
def admission(registry, monkeypatch):
    available = AsyncMock(return_value=[MODEL, "LTX-2.5 Dev BF16"])
    monkeypatch.setattr(videos, "get_available_models", available)
    monkeypatch.setattr(videos.generation_admission, "require_media", lambda *_args: None)
    monkeypatch.setattr(storage, "private_source_bucket", lambda: "synthetic-private-inputs")
    monkeypatch.setattr(storage, "upload_private_source", forbidden)
    monkeypatch.setattr(storage, "upload_source", forbidden)
    monkeypatch.setattr(quota, "check_and_consume", forbidden)
    monkeypatch.setattr(media.credits, "authorize_media", forbidden)
    monkeypatch.setattr(job_queue, "submit_job", forbidden)
    return available


def request(mode, *, video=None, pin=False, **overrides):
    values = {"prompt": "A synthetic red ball moves", "model": MODEL, "frames": 9, "size": "256x256", "seed": 0, **overrides}
    if mode != "generated":
        values["reference_audio"] = base64.b64encode(wav()).decode("ascii")
    if mode == "video":
        values["reference_video"] = base64.b64encode(video).decode("ascii")
    if pin:
        values["keyframes"] = [{"image": image(), "frame": 0, "strength": 0.5}]
    return videos.VideoRequest.model_validate(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [337, 345, 481, 625])
async def test_long_source_requires_one_upgraded_worker_before_paid_side_effects(admission, monkeypatch, frames):
    from grid_api.routers import worker_ws
    from grid_api.services.video_sources import LONG_SOURCE_FEATURE

    body = request("audio", frames=frames)
    body.reference_audio = base64.b64encode(wav(frames)).decode("ascii")
    inputs = await videos.validate_video(body, {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    assert (LONG_SOURCE_FEATURE in plan.payload["required_media_features"]) == (frames > 337)
    assert plan.payload["frames"] == frames
    assert plan.payload["source_media"][0]["samples"] == frames * 2000
    available = AsyncMock(return_value=[])
    monkeypatch.setattr(worker_ws, "get_available_models", available)
    with pytest.raises(HTTPException) as error:
        await media.prepare_dispatch(MODEL, "video", inputs.planning_payload())
    assert error.value.status_code == 503
    assert (LONG_SOURCE_FEATURE in available.call_args.kwargs["media_features"]) == (frames > 337)
    available.return_value = [MODEL]
    payload, _ = await media.prepare_dispatch(MODEL, "video", inputs.planning_payload())
    assert payload["frames"] == frames


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["audio", "video"])
async def test_core_625_frame_sources_reach_the_reviewed_worker(admission, monkeypatch, tmp_path, mode):
    source = os.environ.get("MEDIA_VIDEO_WORKER_SOURCE")
    if not source:
        pytest.skip("reviewed MEDIA_VIDEO_WORKER_SOURCE is required for the joint adapter check")
    worker = Path(source).resolve()
    python = worker / ".venv/bin/python"
    assert python.is_file() and (worker / "bridge/source_media.py").is_file()
    values = dict(prompt="synthetic long source", model=MODEL, frames=625, size="256x256", seed=0,
                  reference_audio=base64.b64encode(wav(625)).decode("ascii"))
    if mode == "video":
        decoder = shutil.which("ffmpeg")
        assert decoder and shutil.which("ffprobe"), "joint source check requires real codecs"
        path = tmp_path / "motion.mp4"
        subprocess.run([decoder, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=256x256:rate=24",
                        "-frames:v", "625", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path)],
                       capture_output=True, timeout=30, check=True)
        values["reference_video"] = base64.b64encode(path.read_bytes()).decode("ascii")
    inputs = await videos.validate_video(videos.VideoRequest(**values), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    monkeypatch.setattr(storage, "upload_private_source", lambda _raw, ext:
                        "https://" + "a" * 32 + ".r2.cloudflarestorage.com/synthetic/source." + ext)
    payload = await plan.stage()
    script = """
import json,sys
sys.path.insert(0, sys.argv[1])
from bridge.source_media import validate_bindings,LONG_SOURCE_FEATURE
body=json.load(sys.stdin)
assert body['frames']==625 and LONG_SOURCE_FEATURE in body['required_media_features']
assert len(validate_bindings(body['recipe_spec'],body))==int(sys.argv[2])
body['required_media_features'].remove(LONG_SOURCE_FEATURE)
try:validate_bindings(body['recipe_spec'],body)
except RuntimeError:pass
else:raise AssertionError('Missing extended capability accepted')
"""
    result = subprocess.run([str(python), "-c", script, str(worker), str(1 if mode == "audio" else 2)],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=30, cwd=tmp_path,
                            env={"PATH": os.environ.get("PATH", ""), "PYTHON_DOTENV_DISABLED": "1", "PYTHONNOUSERSITE": "1"})
    assert result.returncode == 0, "joint long-source adapter failed on synthetic inputs"


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", [{"frames": 625}, {"seconds": 26}])
async def test_old_source_workers_reject_long_admission_before_any_decoder(admission, monkeypatch, timing):
    from grid_api.services.video_sources import LONG_SOURCE_FEATURE

    admission.side_effect = [[MODEL], []]
    monkeypatch.setattr(videos.video_sources, "validate_sources", forbidden)
    body = videos.VideoRequest(prompt="synthetic source", model=MODEL, size="256x256",
                               reference_audio=base64.b64encode(wav(625)).decode("ascii"), **timing)
    with pytest.raises(HTTPException) as error:
        await videos.validate_video(body, {}, async_submission=True)
    assert error.value.status_code == 503
    assert LONG_SOURCE_FEATURE in admission.call_args.kwargs["media_features"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
@pytest.mark.parametrize("pin", [False, True])
async def test_validation_and_compilation_are_side_effect_free(admission, mode, pin, mp4, monkeypatch):
    inputs = await videos.validate_video(request(mode, video=mp4, pin=pin), {}, async_submission=True)
    admission.assert_awaited_once()
    # Compilation itself cannot read the worker registry or revalidate sources.
    monkeypatch.setattr(videos, "get_available_models", forbidden)
    monkeypatch.setattr(videos.video_sources, "validate_sources", forbidden)
    plan = video_plans.compile_video(inputs)
    plan.check_inputs()
    assert plan.snapshot["schema"] == "aipg.video-plan.v1"
    assert plan.sha256 == hashlib.sha256(plan.snapshot_json.encode("ascii")).hexdigest()
    assert plan.payload["seed"] == 0
    assert plan.payload["recipe_spec"]["noise"]["inputs"]["noise_seed"] == 0
    assert plan.payload["recipe_spec"]["latent_video"]["inputs"]["length"] == 9
    assert plan.snapshot["execution"].get("source_mode", "generated") == mode
    assert len(plan.snapshot["inputs"]["images"]) == int(pin)
    assert len(plan.snapshot["inputs"]["sources"]) == {"generated": 0, "audio": 1, "video": 2}[mode]
    assert not any("data:" in value for value in plan.snapshot_json.split())
    assert "synthetic red ball" not in repr(plan)
    assert "synthetic red ball" not in repr(inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [MODEL, "LTX-2.5 Distilled NVFP4"])
@pytest.mark.parametrize("size", ["960x544", "544x960", "480x832"])
async def test_union_geometry_rejects_before_source_decode_or_side_effects(admission, mp4, monkeypatch, model, size):
    monkeypatch.setattr(videos, "get_available_models", AsyncMock(return_value=[model]))
    monkeypatch.setattr(videos.video_sources, "validate_sources", forbidden)
    with pytest.raises(HTTPException) as error:
        await videos.validate_video(request("video", video=mp4, model=model, size=size), {}, async_submission=True)
    assert error.value.status_code == 422
    assert error.value.detail == "Invalid LTX-2.5 inputs; consult the model parameter contract"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
@pytest.mark.parametrize("pin", [False, True])
async def test_staging_uses_the_frozen_graph_after_recipe_changes(admission, mode, pin, mp4, monkeypatch):
    body = request(mode, video=mp4, pin=pin)
    inputs = await videos.validate_video(body, {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    original = plan.snapshot_json
    graph = plan.payload["recipe_spec"]
    commitments = plan.snapshot["inputs"]
    uploads = []

    def upload(raw, ext):
        uploads.append((raw, ext))
        return f"https://source.invalid/{len(uploads)}.{ext}?signature=synthetic"

    monkeypatch.setattr(storage, "upload_private_source", upload)
    monkeypatch.setattr(recipes, "resolve_for_model", forbidden)
    # Neither original request changes nor mutations of returned dict views can
    # alter the snapshotted payload or recipe after planning.
    body.prompt = "Changed after validation"
    body.seed = 42
    view = plan.payload
    view["seed"] = 42
    view["recipe_spec"].clear()
    inputs.payload["prompt"] = "Not a mutable internal dictionary"
    result = await plan.stage()
    assert plan.snapshot_json == original
    assert result["recipe_spec"] == graph
    assert result["prompt"] == "A synthetic red ball moves"
    assert result["seed"] == 0
    assert "video-plan.invalid" not in json.dumps(result)
    assert len(uploads) == int(pin) + {"generated": 0, "audio": 1, "video": 2}[mode]
    for source, expected in zip(result.get("source_media", []), commitments["sources"]):
        assert {key: value for key, value in source.items() if key != "url"} == expected


@pytest.mark.asyncio
async def test_repeated_image_slots_upload_once_without_losing_timing(admission, monkeypatch):
    body = request("generated")
    body.keyframes = [videos.VideoKeyframe(image=image(), frame=0), videos.VideoKeyframe(image=image(), frame=8, strength=0.3)]
    inputs = await videos.validate_video(body, {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    uploads = []
    monkeypatch.setattr(storage, "upload_private_source", lambda raw, ext: uploads.append((raw, ext)) or "https://source.invalid/image.png")
    payload = await plan.stage()
    assert len(uploads) == 1
    assert payload["source_image_urls"] == ["https://source.invalid/image.png"] * 2
    assert payload["recipe_spec"]["guide_1"]["inputs"]["frame_idx"] == 8
    assert payload["recipe_inputs"]["keyframes"][1]["strength"] == 0.3
    assert "recipe-image-bindings-v1" in payload["required_media_features"]


@pytest.mark.asyncio
async def test_changed_source_metadata_rejects_before_first_upload(admission):
    inputs = await videos.validate_video(request("audio"), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    inputs.sources[0].metadata["samples"] = 1
    with pytest.raises(HTTPException) as exc:
        await plan.stage()
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_corrupted_snapshot_rejects_before_first_upload(admission):
    inputs = await videos.validate_video(request("generated", pin=True), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    corrupted = dataclasses.replace(plan, snapshot_json=plan.snapshot_json.replace('"seed":0', '"seed":1'))
    with pytest.raises(HTTPException) as exc:
        await corrupted.stage()
    assert exc.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://video-plan.invalid/image/0",
        "http://source.invalid/image.png",
        "https://user:pass@source.invalid/image.png",
        "https://source.invalid/image.png#x",
    ],
)
async def test_placeholder_or_unsafe_staged_locations_never_dispatch(admission, monkeypatch, url):
    inputs = await videos.validate_video(request("generated", pin=True), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    monkeypatch.setattr(storage, "upload_private_source", lambda *_args: url)
    with pytest.raises(HTTPException) as exc:
        await plan.stage()
    assert exc.value.status_code == 503
    assert url not in exc.value.detail


@pytest.mark.asyncio
async def test_plan_dispatch_bound_rejects_before_storage(admission, monkeypatch):
    inputs = await videos.validate_video(request("generated", pin=True), {}, async_submission=True)
    monkeypatch.setattr(video_plans, "MAX_PAYLOAD_BYTES", 1)
    with pytest.raises(HTTPException) as exc:
        video_plans.compile_video(inputs)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_planning_seed_is_chosen_once_when_not_specified(admission, monkeypatch):
    random = []
    monkeypatch.setattr(media.secrets, "randbelow", lambda limit: random.append(limit) or 42)
    body = request("generated")
    body.__pydantic_extra__["seed"] = None
    inputs = await videos.validate_video(body, {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    first = await plan.stage()
    second = await plan.stage()
    assert len(random) == 1
    assert first == second
    assert first["seed"] == first["recipe_spec"]["noise"]["inputs"]["noise_seed"] == 42


@pytest.mark.asyncio
async def test_plain_opening_image_fits_before_validation_and_staging(admission, monkeypatch):
    body = videos.VideoRequest(prompt="A ball", model=MODEL, image=image(), frames=9)
    inputs = await videos.validate_video(body, {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    expected = video_contract.source_size(recipes.video_contract_for_model(MODEL, has_source=True), 256, 256)
    assert (plan.payload["width"], plan.payload["height"]) == expected
    assert "source_image_urls" not in plan.payload
    monkeypatch.setattr(storage, "upload_private_source", lambda *_args: "https://source.invalid/image.png")
    assert (await plan.stage())["source_image_url"] == "https://source.invalid/image.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unselected_seed", "wrong_seeds", "untyped", "lost_image_slot"])
async def test_unvalidated_plan_inputs_reject_without_selecting_a_seed_or_upload(admission, monkeypatch, change):
    inputs = await videos.validate_video(request("generated", pin=True), {}, async_submission=True)
    monkeypatch.setattr(media.secrets, "randbelow", forbidden)
    if change == "unselected_seed":
        payload = inputs.payload
        payload["seed"] = None
        inputs = dataclasses.replace(inputs, payload_json=json.dumps(payload))
    elif change == "wrong_seeds":
        inputs = dataclasses.replace(inputs, seeds=(1,))
    elif change == "untyped":
        inputs = dataclasses.replace(inputs, private_images=False)
    else:
        inputs = dataclasses.replace(inputs, timed_images=False, images=inputs.images * 2)
    with pytest.raises(HTTPException) as exc:
        video_plans.compile_video(inputs)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_source_metadata_is_copied_before_staging_yields(admission, monkeypatch):
    inputs = await videos.validate_video(request("audio", pin=True), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)

    def upload(_raw, ext):
        # The image upload yields before audio staging; private staged metadata
        # must not follow mutable references held by the original caller.
        inputs.sources[0].metadata["samples"] = 1
        return f"https://source.invalid/frozen.{ext}"

    monkeypatch.setattr(storage, "upload_private_source", upload)
    payload = await plan.stage()
    assert payload["source_media"][0]["samples"] == 18_000
    assert plan.snapshot["inputs"]["sources"][0]["samples"] == 18_000


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["public_upload", "seed_bool", "seed_tuple"])
async def test_frozen_private_plan_cannot_switch_upload_policy_or_primitive_identity(admission, change):
    inputs = await videos.validate_video(request("generated", pin=True), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    if change == "public_upload":
        inputs = dataclasses.replace(inputs, private_images=False)
    elif change == "seed_tuple":
        inputs = dataclasses.replace(inputs, seeds=(False,))
    else:
        payload = inputs.payload
        payload["seed"] = False
        inputs = dataclasses.replace(inputs, payload_json=json.dumps(payload))
    plan = dataclasses.replace(plan, inputs=inputs)
    with pytest.raises(HTTPException) as error:
        await plan.stage()
    assert error.value.status_code == 422
