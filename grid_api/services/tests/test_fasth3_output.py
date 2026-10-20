# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic output-clock faults and real CPU decode, never native generation."""

import hashlib
import json
import shutil
import subprocess
from fractions import Fraction
from unittest.mock import Mock

import pytest

from grid_api.services import fasth3_output as output
from grid_api.services import storage

RAW = bytes.fromhex("000000186674797069736f6d0000020069736f6d69736f32")


def metadata(frames=124, rate=32_000, size=(1344, 768)):
    duration = Fraction(frames, 24)
    samples = (frames * rate + 23) // 24
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": size[0],
                "height": size[1],
                "pix_fmt": "yuv420p",
                "sample_aspect_ratio": "1:1",
                "avg_frame_rate": "24/1",
                "r_frame_rate": "24/1",
                "nb_frames": str(frames),
                "time_base": "1/12288",
                "start_pts": 0,
                "start_time": "0.000000",
                "duration_ts": frames * 512,
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": str(rate),
                "channels": 2,
                "time_base": f"1/{rate}",
                "start_pts": 0,
                "start_time": "0.000000",
                "duration_ts": samples,
            },
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "start_time": "0.000000",
            "duration": f"{float(max(duration, Fraction(samples, rate))):.6f}",
        },
    }


def video_rows(frames=124, size=(1344, 768)):
    headers = ["#tb 0: 1/24", "#media_type 0: video", "#codec_id 0: rawvideo", f"#dimensions 0: {size[0]}x{size[1]}"]
    return ("\n".join(headers + [f"0, {i}, {i}, 1, {size[0] * size[1] * 3 // 2}, " + "a" * 32 for i in range(frames)]) + "\n").encode()


def audio_rows(frames=124, rate=32_000, *, samples=None):
    if samples is None:
        presented = (frames * rate + 23) // 24
        samples = ((presented + 1023) // 1024) * 1024
    headers = [
        f"#tb 0: 1/{rate}",
        "#media_type 0: audio",
        "#codec_id 0: pcm_s16le",
        f"#sample_rate 0: {rate}",
        "#channel_layout_name 0: stereo",
    ]
    rows = [f"0, {i}, {i}, {min(4096, samples - i)}, {min(4096, samples - i) * 4}, " + "b" * 32 for i in range(0, samples, 4096)]
    return ("\n".join(headers + rows) + "\n").encode()


@pytest.fixture
def codecs(monkeypatch):
    calls = []
    values = {"metadata": metadata(), "video": video_rows(), "audio": audio_rows()}

    def run(tool, args, directory, **bounds):
        calls.append((tool, args, directory, bounds))
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "output.mp4").stat().st_mode & 0o777 == 0o600
        assert bounds["timeout"] <= 30 and bounds["limit"] <= 65536
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert args[args.index("-enable_drefs") + 1] == "0"
        assert args[args.index("-use_absolute_path") + 1] == "0"
        return json.dumps(values["metadata"]).encode() if tool == "ffprobe" else values["video" if "-an" in args else "audio"]

    monkeypatch.setattr(output.media_decoder, "available", lambda: True)
    monkeypatch.setattr(output.media_decoder, "run_tool", run)

    def no_storage():
        pytest.fail("offline output inspection must never use storage")

    monkeypatch.setattr(storage, "_client", no_storage)
    return values, calls


def inspect(raw=RAW, **inputs):
    return output.inspect_candidate(raw, inputs={"prompt": "Synthetic output check.", **inputs})


@pytest.mark.parametrize("frames,rate,size", [(124, 32_000, (1344, 768)), (141, 48_000, (768, 1344)), (362, 32_000, (768, 768))])
def test_exact_presentation_clock_and_full_untrimmed_decode_are_separate(codecs, frames, rate, size):
    values, calls = codecs
    values.update(metadata=metadata(frames, rate, size), video=video_rows(frames, size), audio=audio_rows(frames, rate))
    options = {"appearance_image": True, "audio_mode": "preserve"} if rate == 48_000 else {}
    result = inspect(frames=frames, width=size[0], height=size[1], **options)
    assert result.sha256 == hashlib.sha256(RAW).hexdigest() and result.bytes == len(RAW)
    assert result.frames == frames and result.audio_sample_rate == rate
    assert Fraction(result.duration_numerator, result.duration_denominator) == Fraction(frames, 24)
    assert result.audio_presentation_samples == (frames * rate + 23) // 24
    assert 0 <= result.aac_padding_samples <= 1023
    assert result.audio_decoded_samples == result.audio_presentation_samples + result.aac_padding_samples
    assert result.qualification_only is True and len(calls) == 3
    assert all(not path.exists() for _, _, path, _ in calls)
    audio_args = calls[-1][1]
    assert "atrim" not in " ".join(audio_args) and "-t" not in audio_args
    assert audio_args[audio_args.index("-af") + 1] == "asetnsamples=n=4096:p=0"


@pytest.mark.parametrize(
    "path,value",
    [
        (("streams",), []),
        (("streams", 0, "codec_name"), "hevc"),
        (("streams", 0, "width"), 768),
        (("streams", 0, "pix_fmt"), "yuv444p"),
        (("streams", 0, "nb_frames"), "125"),
        (("streams", 0, "avg_frame_rate"), "30/1"),
        (("streams", 0, "r_frame_rate"), "30/1"),
        (("streams", 0, "start_pts"), 1),
        (("streams", 0, "start_time"), "0.1"),
        (("streams", 0, "duration_ts"), 63489),
        (("streams", 0, "time_base"), "0/1"),
        (("streams", 0, "sample_aspect_ratio"), "2:1"),
        (("streams", 0, "side_data_list"), [{"rotation": 90}]),
        (("streams", 0, "disposition"), {"attached_pic": 1}),
        (("streams", 1, "codec_name"), "opus"),
        (("streams", 1, "profile"), "HE-AAC"),
        (("streams", 1, "sample_rate"), "48000"),
        (("streams", 1, "channels"), 1),
        (("streams", 1, "start_pts"), 1024),
        (("streams", 1, "duration_ts"), 165335),
        (("streams", 1, "time_base"), "1/48000"),
        (("streams", 1, "duration_ts"), True),
        (("format", "duration"), "5.2"),
        (("format", "duration"), "nan"),
        (("format", "duration"), "1e999999999"),
        (("streams", 0, "time_base"), "1/" + "9" * 60),
        (("format", "start_time"), "1"),
        (("format", "format_name"), "matroska,webm"),
    ],
)
def test_inconsistent_probe_rejects_before_any_full_decode(codecs, path, value):
    values, calls = codecs
    target = values["metadata"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(output.OutputRejected):
        inspect()
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["count", "gap", "size", "hash", "clock", "geometry"])
def test_full_frame_observations_cannot_be_replaced_by_probe_counts(codecs, case):
    values, calls = codecs
    changes = {
        "count": lambda: video_rows(123),
        "gap": lambda: video_rows().replace(b"0, 1, 1,", b"0, 2, 2,", 1),
        "size": lambda: video_rows().replace(b"1548288", b"1548287", 1),
        "hash": lambda: video_rows().replace(b"a" * 32, b"not a digest", 1),
        "clock": lambda: video_rows().replace(b"1/24", b"1/30", 1),
        "geometry": lambda: video_rows(size=(768, 768)),
    }
    values["video"] = changes[case]()
    with pytest.raises(output.OutputRejected):
        inspect()
    assert len(calls) == 2


@pytest.mark.parametrize("samples", [165333, 166358])
def test_audio_decode_cannot_underrun_presentation_or_hide_multiple_padding_blocks(codecs, samples):
    values, _ = codecs
    values["audio"] = audio_rows(samples=samples)
    with pytest.raises(output.OutputRejected):
        inspect()


def test_audio_requires_contiguous_full_decode_not_just_correct_metadata(codecs):
    values, _ = codecs
    values["audio"] = audio_rows().replace(b"0, 4096, 4096,", b"0, 8192, 8192,", 1)
    with pytest.raises(output.OutputRejected):
        inspect()


@pytest.mark.parametrize(
    "fault",
    [
        OSError("private path"),
        subprocess.TimeoutExpired("private command", 30),
        subprocess.CalledProcessError(1, "private path"),
        ValueError("private diagnostics"),
    ],
)
def test_tool_failures_remain_uncertain_and_redacted(codecs, monkeypatch, fault):
    tool = Mock(side_effect=fault)
    gate = Mock()
    gate.acquire.return_value = True
    monkeypatch.setattr(output, "_SLOTS", gate)
    monkeypatch.setattr(output.media_decoder, "run_tool", tool)
    with pytest.raises(output.OutputCheckUnavailable) as error:
        inspect()
    assert "private" not in str(error.value)
    assert tool.call_count == 1
    gate.release.assert_called_once_with()


def test_missing_codecs_or_busy_capacity_never_decode(codecs, monkeypatch):
    _, calls = codecs
    monkeypatch.setattr(output.media_decoder, "available", lambda: False)
    with pytest.raises(output.OutputCheckUnavailable):
        inspect()
    monkeypatch.setattr(output.media_decoder, "available", lambda: True)
    gate = Mock()
    gate.acquire.return_value = False
    monkeypatch.setattr(output, "_SLOTS", gate)
    with pytest.raises(output.OutputCheckUnavailable):
        inspect()
    assert not calls
    gate.release.assert_not_called()


@pytest.mark.parametrize("raw", [b"", b"not a video", RAW[:16], b"\0\0\0\x11" + RAW[4:]])
def test_invalid_bytes_reject_without_probe(codecs, raw):
    _, calls = codecs
    with pytest.raises(output.OutputRejected):
        inspect(raw)
    assert not calls


def test_input_contract_rejects_before_codec_capacity(codecs):
    _, calls = codecs
    with pytest.raises(ValueError):
        inspect(frames=125)
    assert not calls


@pytest.fixture(scope="module")
def synthetic_clips(tmp_path_factory):
    if not output.media_decoder.available():
        pytest.skip("bounded CPU codec verification unavailable")
    decoder = shutil.which("ffmpeg")
    assert decoder
    root, clips = tmp_path_factory.mktemp("h3-output"), {}
    for rate in (32_000, 48_000):
        path = root / f"synthetic-{rate}.mp4"
        subprocess.run(
            [
                decoder,
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:size=768x768:rate=24",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate={rate}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-frames:v",
                "124",
                "-t",
                str(124 / 24),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-movie_timescale",
                "96000",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        clips[rate] = path.read_bytes()
    return clips


@pytest.mark.parametrize("rate", [32_000, 48_000])
def test_actual_cpu_probe_and_full_decode_of_both_output_clocks(synthetic_clips, rate):
    options = {"appearance_image": True, "audio_mode": "preserve"} if rate == 48_000 else {}
    result = inspect(synthetic_clips[rate], width=768, height=768, **options)
    assert result.frames == 124 and result.audio_sample_rate == rate
    assert result.sha256 == hashlib.sha256(synthetic_clips[rate]).hexdigest()
    assert abs(Fraction(result.audio_presentation_samples, rate) - Fraction(124, 24)) <= Fraction(1, rate)


def test_real_decoder_corruption_never_becomes_a_passing_receipt(synthetic_clips):
    with pytest.raises((output.OutputRejected, output.OutputCheckUnavailable)):
        inspect(synthetic_clips[32_000][:1024], width=768, height=768)


@pytest.mark.parametrize("count", [1, 3])
def test_missing_or_extra_media_streams_are_not_silent_downgrades(codecs, count):
    values, calls = codecs
    streams = values["metadata"]["streams"]
    values["metadata"]["streams"] = streams[:1] if count == 1 else [*streams, dict(streams[1])]
    with pytest.raises(output.OutputRejected):
        inspect()
    assert len(calls) == 1


def test_raw_byte_bound_rejects_without_allocating_a_large_fixture(codecs, monkeypatch):
    _, calls = codecs
    monkeypatch.setattr(output, "MAX_VIDEO_BYTES", 16)
    with pytest.raises(output.OutputRejected):
        inspect()
    assert not calls


def test_invalid_codec_response_type_is_unavailable_not_a_paid_result(codecs, monkeypatch):
    monkeypatch.setattr(output.media_decoder, "run_tool", lambda *_args, **_kwargs: "not bytes")
    with pytest.raises(output.OutputCheckUnavailable):
        inspect()


def test_actual_maximum_portrait_frame_and_audio_observations_fit_the_codec_bounds(tmp_path):
    if not output.media_decoder.available():
        pytest.skip("bounded CPU codec verification unavailable")
    decoder = shutil.which("ffmpeg")
    assert decoder
    path = tmp_path / "synthetic-boundary.mp4"
    subprocess.run(
        [
            decoder,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=768x1344:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-frames:v",
            "362",
            "-t",
            str(362 / 24),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-movie_timescale",
            "96000",
            str(path),
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    result = inspect(path.read_bytes(), frames=362, width=768, height=1344, appearance_image=True, audio_mode="preserve")
    assert result.frames == 362 and result.audio_sample_rate == 48_000
    assert Fraction(result.duration_numerator, result.duration_denominator) == Fraction(181, 12)
    assert abs(result.audio_presentation_samples - 724_000) <= 1
    assert 0 <= result.aac_padding_samples <= 1023
