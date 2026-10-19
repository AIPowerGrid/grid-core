# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import base64
import copy
import io
import shutil
import subprocess
import wave

import pytest
from fastapi import HTTPException

from grid_api.services import video_sources


def wav(frames=9, rate=48_000, channels=2, width=2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\0" * (frames * 2000 * channels * width))
    return stream.getvalue()


def test_audio_commits_exact_complete_stereo_pcm():
    raw = wav()
    source = video_sources.validate_audio(raw, 9)
    assert source.commitment["samples"] == 18_000
    assert source.commitment["frames"] == 9
    assert source.commitment["bytes"] == len(raw)
    assert "data" not in source.commitment
    assert video_sources.decode_inline("data:audio/wav;base64," + base64.b64encode(raw).decode(), "reference_audio") == raw


@pytest.mark.parametrize("frames", [241, 337])
def test_long_soundtrack_requires_the_complete_selected_window(frames):
    raw = wav(frames)
    source = video_sources.validate_audio(raw, frames)
    assert source.commitment["samples"] == frames * 2000
    assert source.commitment["frames"] == frames
    for short in (wav(169), raw[:-4]):
        with pytest.raises(HTTPException):
            video_sources.validate_audio(short, frames)
    with pytest.raises(HTTPException):
        video_sources.validate_audio(wav(345), 345)


@pytest.mark.parametrize("frames", [345, 481, 625])
def test_generated_duration_extension_does_not_widen_source_workers(frames):
    with pytest.raises(HTTPException) as exc:
        video_sources.validate_audio(wav(frames), frames)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("raw,frames", [
    (wav(17), 9), (wav(rate=44_100), 9), (wav(channels=1), 9), (wav(width=1), 9),
    (wav()[:-2], 9), (wav() + b"trailing payload", 9), (b"not-a-wav", 9), (wav(), True),
])
def test_audio_rejects_incomplete_wrong_format_or_wrong_duration(raw, frames):
    with pytest.raises(HTTPException) as exc:
        video_sources.validate_audio(raw, frames)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("value", ["https://private.invalid/input.wav", "invalid private input",
                                      "data:video/mp4;base64,YQ==", "data:audio/wav,YQ=="])
def test_inline_sources_never_fetch_or_reflect_bad_input(value):
    with pytest.raises(HTTPException) as exc:
        video_sources.decode_inline(value, "reference_audio")
    assert value not in exc.value.detail


@pytest.fixture
def mp4(tmp_path):
    decoder = shutil.which("ffmpeg")
    if not decoder or not shutil.which("ffprobe"):
        pytest.skip("independent FFmpeg source-decoder gate unavailable")
    path = tmp_path / "synthetic.mp4"
    subprocess.run([decoder, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=256x256:rate=24",
                    "-frames:v", "9", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path)],
                   capture_output=True, timeout=10, check=True)
    return path.read_bytes()


def test_source_video_is_independently_decoded(mp4):
    source = video_sources.validate_video(mp4, 9, 256, 256)
    assert source.role == "reference_video"
    assert source.commitment["frames"] == 9
    assert source.commitment["width"] == source.commitment["height"] == 256


@pytest.mark.parametrize("frames,width,height", [(17, 256, 256), (9, 512, 256), (9, 256, 512)])
def test_source_video_rejects_mismatched_execution(mp4, frames, width, height):
    with pytest.raises(HTTPException) as exc:
        video_sources.validate_video(mp4, frames, width, height)
    assert exc.value.status_code == 422


def test_corrupt_source_is_not_a_header_only_success(mp4):
    with pytest.raises(HTTPException):
        video_sources.validate_video(mp4[:64], 9, 256, 256)


def test_missing_decoder_fails_closed(monkeypatch, mp4):
    monkeypatch.setattr(video_sources.shutil, "which", lambda *_args: None)
    with pytest.raises(HTTPException) as exc:
        video_sources.validate_video(mp4, 9, 256, 256)
    assert exc.value.status_code == 503


def commitment():
    source = video_sources.validate_audio(wav(), 9)
    return [{**source.commitment, "url": "https://source.invalid/owned.wav"}]


def test_source_roles_are_bound_to_exact_execution_before_money():
    sources = commitment()
    video_sources.validate_commitments(sources, [{"role": "reference_audio", "path": "source_audio.inputs.audio"}],
                                      {"source_mode": "audio", "frames": 9, "width": 256, "height": 256})


@pytest.mark.parametrize("change", [{"frames": 17}, {"samples": 1}, {"sha256": "not-a-digest"},
                                      {"url": "http://source.invalid/owned.wav"}, {"url": "https://user:secret@source.invalid/a.wav"},
                                      {"bytes": True}, {"fps": True}, {"extra": "private"}])
def test_bad_commitments_reject_before_money(change):
    sources = copy.deepcopy(commitment())
    sources[0].update(change)
    with pytest.raises(HTTPException) as exc:
        video_sources.validate_commitments(sources, [{"role": "reference_audio", "path": "source_audio.inputs.audio"}],
                                          {"source_mode": "audio", "frames": 9, "width": 256, "height": 256})
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_video_reference_requires_explicit_audio_before_decoder(monkeypatch):
    def forbidden(*_args):
        pytest.fail("decoder must not run")
    monkeypatch.setattr(video_sources, "validate_video", forbidden)
    with pytest.raises(HTTPException) as exc:
        await video_sources.validate_sources(None, "private", frames=9, width=256, height=256)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_cancelled_request_keeps_decoder_slot_until_native_verification_exits(monkeypatch):
    gate = asyncio.Semaphore(1)
    started, finish = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(video_sources, "_VIDEO_CHECKS", gate)

    async def slow_thread(_fn, *_args):
        started.set()
        await finish.wait()
        return video_sources.Source("reference_video", b"synthetic", "mp4",
                                   {"width": 256, "height": 256, "frames": 9, "fps": 24})

    monkeypatch.setattr(video_sources.asyncio, "to_thread", slow_thread)
    audio = base64.b64encode(wav()).decode()
    task = asyncio.create_task(video_sources.validate_sources(audio, "c3ludGhldGlj",
                              frames=9, width=256, height=256))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.locked()
    finish.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not gate.locked():
            break
    assert not gate.locked()
