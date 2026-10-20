# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline H3 output inspection; never admission, model fidelity or money authority.

Call synchronously outside the event loop. This deliberately does not plug into
the live S3 freeze/settlement path; that needs a reviewed immutable-byte handoff.
"""

import csv
import hashlib
import json
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import media_decoder
from .fasth3_contract import compile_candidate
from .video_output import MAX_VIDEO_BYTES

_SLOTS = threading.BoundedSemaphore(2)


class OutputRejected(ValueError):
    """A completed observation violates this candidate's media contract."""


class OutputCheckUnavailable(RuntimeError):
    """Verification cannot establish a result; never infer payment or refund."""


@dataclass(frozen=True)
class OutputEvidence:
    sha256: str
    bytes: int
    frames: int
    width: int
    height: int
    duration_numerator: int
    duration_denominator: int
    audio_sample_rate: int
    audio_presentation_samples: int
    audio_decoded_samples: int
    aac_padding_samples: int
    qualification_only: bool = True


def _integer(value: Any) -> int:
    if type(value) is int and 0 <= value < 2**63:
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,17}", value):
        return int(value)
    raise ValueError("invalid integer")


def _rational(value: Any) -> Fraction:
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12}|/[1-9][0-9]{0,17})?", value):
        raise ValueError("invalid rational")
    return Fraction(value)


def _run(tool: str, args: list[str], directory: Path, *, timeout: int, limit: int) -> bytes:
    try:
        data = media_decoder.run_tool(tool, args, directory, timeout=timeout, limit=limit)
        if not isinstance(data, bytes) or len(data) > limit:
            raise ValueError("invalid decoder response")
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        # Decoder exit can also mean namespace, resource or runtime failure.
        # Do not misclassify it as definitive evidence to refund a held job.
        raise OutputCheckUnavailable("FastH3 output verification unavailable") from None


def _probe(raw: bytes, execution: dict[str, Any]) -> int:
    try:
        metadata = json.loads(raw)
        streams = metadata["streams"]
        if not isinstance(streams, list) or len(streams) != 2:
            raise ValueError
        videos = [stream for stream in streams if stream["codec_type"] == "video"]
        audios = [stream for stream in streams if stream["codec_type"] == "audio"]
        if len(videos) != 1 or len(audios) != 1:
            raise ValueError
        video, audio = videos[0], audios[0]
        frames, width, height = (execution[key] for key in ("frames", "width", "height"))
        duration, rate = Fraction(frames, 24), execution["output_audio_sample_rate"]
        if (
            video["codec_name"] != "h264"
            or type(video["width"]) is not int
            or video["width"] != width
            or type(video["height"]) is not int
            or video["height"] != height
            or video["pix_fmt"] != "yuv420p"
            or _integer(video["nb_frames"]) != frames
            or _rational(video["avg_frame_rate"]) != 24
            or _rational(video["r_frame_rate"]) != 24
            or _integer(video["start_pts"]) != 0
            or _rational(video["start_time"]) != 0
            or _rational(video["time_base"]) <= 0
            or _integer(video["duration_ts"]) * _rational(video["time_base"]) != duration
            or video.get("sample_aspect_ratio") not in (None, "N/A", "1:1")
            or video.get("disposition", {}).get("attached_pic", 0)
            or any(item.get("rotation", 0) != 0 for item in video.get("side_data_list", []))
            or audio["codec_name"] != "aac"
            or audio.get("profile") != "LC"
            or _integer(audio["sample_rate"]) != rate
            or type(audio["channels"]) is not int
            or audio["channels"] != 2
            or _integer(audio["start_pts"]) != 0
            or _rational(audio["start_time"]) != 0
            or _rational(audio["time_base"]) != Fraction(1, rate)
        ):
            raise ValueError
        samples = _integer(audio["duration_ts"])
        if abs(Fraction(samples, rate) - duration) > Fraction(1, rate):
            raise ValueError
        container = metadata["format"]
        if (
            "mp4" not in container["format_name"].split(",")
            or _rational(container["start_time"]) != 0
            # MP4's movie timescale may round the overall duration to a ms.
            # Track clocks above, not this display field, prove synchronization.
            or abs(_rational(container["duration"]) - max(duration, Fraction(samples, rate))) > Fraction(1, 1000)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
        raise OutputRejected("FastH3 output metadata violates the candidate contract") from None
    return samples


def _rows(raw: bytes, headers: set[str]) -> list[list[str]]:
    try:
        lines = raw.decode("ascii").splitlines()
        if not headers.issubset(set(lines)):
            raise ValueError
        rows = list(csv.reader(line for line in lines if line and not line.startswith("#")))
        if not rows or len(rows) > 400:
            raise ValueError
        for row in rows:
            if len(row) != 6 or not re.fullmatch(r"[0-9a-f]{32}", row[5].strip()):
                raise ValueError
    except (UnicodeError, ValueError, csv.Error):
        raise OutputRejected("FastH3 output decode observation is invalid") from None
    return rows


def _video_frames(raw: bytes, execution: dict[str, Any]) -> None:
    width, height, frames = (execution[key] for key in ("width", "height", "frames"))
    rows = _rows(raw, {"#tb 0: 1/24", "#media_type 0: video", "#codec_id 0: rawvideo", f"#dimensions 0: {width}x{height}"})
    try:
        if len(rows) != frames:
            raise ValueError
        for index, row in enumerate(rows):
            if [_integer(item.strip()) for item in row[:5]] != [0, index, index, 1, width * height * 3 // 2]:
                raise ValueError
    except ValueError:
        raise OutputRejected("FastH3 decoded video violates the candidate frame clock") from None


def _audio_samples(raw: bytes, rate: int, presented: int) -> int:
    rows = _rows(
        raw,
        {f"#tb 0: 1/{rate}", "#media_type 0: audio", "#codec_id 0: pcm_s16le", f"#sample_rate 0: {rate}", "#channel_layout_name 0: stereo"},
    )
    total = 0
    try:
        for row in rows:
            stream, dts, pts, count, size = (_integer(item.strip()) for item in row[:5])
            if stream != 0 or dts != total or pts != total or not 0 < count <= 4096 or size != count * 4:
                raise ValueError
            total += count
        # Decode everything without trimming. AAC can pad one terminal 1024-
        # sample block; its presentation clock must independently pass _probe.
        if not presented <= total <= presented + 1023:
            raise ValueError
    except ValueError:
        raise OutputRejected("FastH3 decoded audio violates the candidate sample clock") from None
    return total


def inspect_candidate(raw: bytes, *, inputs: dict[str, Any]) -> OutputEvidence:
    """Inspect exact local bytes; no upload, model, settlement or admission calls.

    Missing/failed codecs or capacity are uncertain, not failed-render evidence.
    Passing proves bounded media conformance only, never requested identity,
    soundtrack fidelity, lip sync, model weights or production readiness.
    """
    execution = compile_candidate(inputs)["execution"]
    if (
        type(raw) is not bytes
        or not 16 <= len(raw) <= MAX_VIDEO_BYTES
        or raw[4:8] != b"ftyp"
        or not 16 <= int.from_bytes(raw[:4], "big") <= len(raw)
        or int.from_bytes(raw[:4], "big") % 4
    ):
        raise OutputRejected("FastH3 output is outside its byte contract")
    if not media_decoder.available():
        raise OutputCheckUnavailable("FastH3 output verification unavailable")
    if not _SLOTS.acquire(timeout=5):
        raise OutputCheckUnavailable("FastH3 output verification capacity unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="grid-fasth3-output-") as directory:
            workspace = Path(directory)
            path = workspace / "output.mp4"
            path.write_bytes(raw)
            path.chmod(0o600)
            base = [
                "-v",
                "error",
                "-max_alloc",
                "67108864",
                "-threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                "-f",
                "mov",
            ]
            metadata = _run(
                "ffprobe",
                [
                    *base,
                    "-show_entries",
                    "stream=codec_type,codec_name,profile,width,height,pix_fmt,sample_aspect_ratio,avg_frame_rate,r_frame_rate,"
                    "nb_frames,time_base,start_pts,start_time,duration_ts,sample_rate,channels:stream_side_data=rotation:"
                    "stream_disposition=attached_pic:format=format_name,duration,start_time",
                    "-of",
                    "json",
                    str(path),
                ],
                workspace,
                timeout=10,
                limit=32768,
            )
            presented = _probe(metadata, execution)
            decode = ["-nostdin", "-xerror", "-err_detect", "explode", *base, "-i", str(path)]
            frames = _run(
                "ffmpeg",
                [
                    *decode,
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-dn",
                    "-threads",
                    "1",
                    "-frames:v",
                    str(execution["frames"] + 1),
                    "-fps_mode",
                    "passthrough",
                    "-c:v",
                    "rawvideo",
                    "-pix_fmt",
                    "yuv420p",
                    "-f",
                    "framemd5",
                    "-",
                ],
                workspace,
                timeout=30,
                limit=65536,
            )
            _video_frames(frames, execution)
            sound = _run(
                "ffmpeg",
                [
                    *decode,
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-threads",
                    "1",
                    "-af",
                    "asetnsamples=n=4096:p=0",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "framemd5",
                    "-",
                ],
                workspace,
                timeout=30,
                limit=65536,
            )
            decoded = _audio_samples(sound, execution["output_audio_sample_rate"], presented)
    except OSError:
        raise OutputCheckUnavailable("FastH3 output verification unavailable") from None
    finally:
        _SLOTS.release()
    return OutputEvidence(
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        frames=execution["frames"],
        width=execution["width"],
        height=execution["height"],
        duration_numerator=execution["duration_numerator"],
        duration_denominator=execution["duration_denominator"],
        audio_sample_rate=execution["output_audio_sample_rate"],
        audio_presentation_samples=presented,
        audio_decoded_samples=decoded,
        aac_padding_samples=decoded - presented,
    )
