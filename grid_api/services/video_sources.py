# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private, bounded source inputs for the governed LTX audio/video candidates.

Callers supply inline bytes, never URLs, filenames or executable graphs. This
module validates already-normalized sources; it does not silently transcode,
trim or stretch content after the client has committed its request identity.
"""

import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from PIL import Image

from . import storage

MAX_SOURCE_FRAMES = 625
LEGACY_SOURCE_FRAMES = 337
LONG_SOURCE_FEATURE = "recipe-media-sources-625-v1"

MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_ENCODED_BYTES = 64 * 1024 * 1024
_VIDEO_CHECKS = asyncio.Semaphore(2)


@dataclass(frozen=True)
class Source:
    role: str
    data: bytes
    ext: str
    metadata: dict

    @property
    def commitment(self) -> dict:
        return {"role": self.role, "sha256": hashlib.sha256(self.data).hexdigest(),
                "bytes": len(self.data), "content_type": storage.CONTENT_TYPES[self.ext],
                **self.metadata}


def decode_inline(value: str, role: str) -> bytes:
    """Strict base64/data URI decoding without reflecting private source bytes."""
    if role not in {"reference_audio", "reference_video", "reference_image"}:
        raise ValueError("invalid source role")
    limit, mime = ((MAX_IMAGE_BYTES, "image/png") if role == "reference_image"
                   else (MAX_AUDIO_BYTES, "audio/wav") if role == "reference_audio"
                   else (MAX_VIDEO_BYTES, "video/mp4"))
    if not isinstance(value, str) or not value or len(value) > 4 * ((limit + 2) // 3) + 64:
        raise HTTPException(413, detail="Video conditioning source exceeds its byte limit")
    if value.startswith("data:"):
        prefix = f"data:{mime};base64,"
        if not value.startswith(prefix):
            raise HTTPException(422, detail="Source media type does not match its role")
        value = value[len(prefix):]
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, detail="Invalid inline video conditioning source") from None
    if not raw or len(raw) > limit:
        raise HTTPException(413, detail="Video conditioning source exceeds its byte limit")
    return raw


def validate_image(raw: bytes) -> Source:
    """Require the exact, fully decoded PNG sheet; never crop/transcode silently."""
    try:
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (768, 448):
                raise ValueError
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            if image.n_frames != 1 or image.getexif().get(274, 1) != 1:
                raise ValueError
            image.load()
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
        raise HTTPException(422, detail="Appearance reference must be a complete single-frame 768x448 PNG") from None
    return Source("reference_image", raw, "png", {"width": 768, "height": 448})


def validate_audio(raw: bytes, frames: int) -> Source:
    """Require exact 48 kHz stereo PCM16 duration, with complete RIFF chunks."""
    try:
        if (type(frames) is not int or not 9 <= frames <= MAX_SOURCE_FRAMES or frames % 8 != 1
                or len(raw) > MAX_AUDIO_BYTES or len(raw) < 44 or raw[:4] != b"RIFF"
                or raw[8:12] != b"WAVE" or struct.unpack_from("<I", raw, 4)[0] != len(raw) - 8):
            raise ValueError
        offset, chunks = 12, []
        while offset < len(raw):
            if offset + 8 > len(raw):
                raise ValueError
            kind, size = raw[offset:offset + 4], struct.unpack_from("<I", raw, offset + 4)[0]
            offset += 8 + size + size % 2
            if offset > len(raw) or kind not in {b"fmt ", b"data", b"LIST", b"JUNK"}:
                raise ValueError
            chunks.append(kind)
        if chunks.count(b"fmt ") != 1 or chunks.count(b"data") != 1:
            raise ValueError
        with wave.open(io.BytesIO(raw)) as reader:
            expected = frames * 2000  # 48,000 samples / 24 video frames.
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getcomptype(),
                    reader.getnframes()) != (2, 2, 48_000, "NONE", expected):
                raise ValueError
            if len(reader.readframes(expected + 1)) != expected * 4:
                raise ValueError
    except (ValueError, EOFError, wave.Error, struct.error):
        raise HTTPException(422, detail="Reference audio must be complete exact-duration 48 kHz stereo PCM16 WAV") from None
    return Source("reference_audio", raw, "wav", {"sample_rate": 48_000, "channels": 2,
                                                 "samples": expected, "frames": frames, "fps": 24})


def validate_video(raw: bytes, frames: int, width: int, height: int) -> Source:
    """Independently probe and decode a contained H.264 MP4, never a URL."""
    from . import media_decoder

    probe, decoder = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not probe or not decoder or not media_decoder.available():
        raise HTTPException(503, detail="Video source verification is unavailable")
    try:
        if (len(raw) > MAX_VIDEO_BYTES or len(raw) < 16 or raw[4:8] != b"ftyp"
                or not 9 <= frames <= MAX_SOURCE_FRAMES or frames % 8 != 1
                or not 256 <= width <= 960 or not 256 <= height <= 960
                or width % 32 or height % 32 or width * height > 960 * 544):
            raise ValueError
        with tempfile.TemporaryDirectory(prefix="grid-video-source-") as directory:
            path = Path(directory) / "source.mp4"
            path.write_bytes(raw)
            path.chmod(0o600)
            result = media_decoder.run_tool("ffprobe", [
                "-v", "error", "-max_alloc", "67108864", "-threads", "1",
                "-protocol_whitelist", "file,pipe", "-enable_drefs", "0", "-use_absolute_path", "0", "-f", "mov",
                "-show_entries", ("stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,"
                                  "nb_frames,sample_rate,channels,start_time:stream_side_data=rotation:"
                                  "stream_disposition=attached_pic:format=format_name,duration,start_time"),
                "-of", "json", str(path),
            ], Path(directory), timeout=10, limit=32*1024)
            metadata = json.loads(result)
            streams = metadata["streams"]
            video = [item for item in streams if item["codec_type"] == "video"]
            audio = [item for item in streams if item["codec_type"] == "audio"]
            if len(video) != 1 or len(audio) > 1 or len(streams) != len(video) + len(audio):
                raise ValueError
            v = video[0]
            if ((v["codec_name"], v["width"], v["height"], v["pix_fmt"], int(v["nb_frames"]))
                    != ("h264", width, height, "yuv420p", frames)
                    or Fraction(v["avg_frame_rate"]) != 24 or Fraction(v["r_frame_rate"]) != 24
                    or float(v.get("start_time", 0)) != 0
                    or v.get("disposition", {}).get("attached_pic", 0)
                    or any(item.get("rotation", 0) != 0 for item in v.get("side_data_list", []))):
                raise ValueError
            if audio and (audio[0].get("codec_name") != "aac" or int(audio[0]["sample_rate"]) != 48_000
                          or audio[0].get("channels") != 2):
                raise ValueError
            duration = float(metadata["format"]["duration"])
            if not math.isfinite(duration) or abs(duration - frames / 24) > 0.05:
                raise ValueError
            decoded = media_decoder.run_tool("ffmpeg", [
                "-v", "error", "-nostdin", "-xerror", "-err_detect", "explode",
                "-max_alloc", "67108864", "-threads", "1", "-protocol_whitelist", "file,pipe",
                "-enable_drefs", "0", "-use_absolute_path", "0", "-f", "mov", "-i", str(path),
                "-map", "0:v:0", "-an", "-sn", "-dn", "-threads", "1", "-frames:v", str(frames + 1), "-fps_mode", "passthrough",
                "-f", "framemd5", "-",
            ], Path(directory), timeout=30, limit=64*1024)
            lines = decoded.decode("ascii").splitlines()
            if "#tb 0: 1/24" not in lines:
                raise ValueError
            rows = [line.split(",") for line in lines if line and not line.startswith("#")]
            if len(rows) != frames:
                raise ValueError
            for index, row in enumerate(rows):
                if len(row) != 6 or [int(item.strip()) for item in row[:4]] != [0, index, index, 1]:
                    raise ValueError
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError, OSError,
            subprocess.SubprocessError, ZeroDivisionError):
        raise HTTPException(422, detail="Reference video must decode as exact-frame 24 fps H.264 MP4 at the requested size") from None
    return Source("reference_video", raw, "mp4", {"width": width, "height": height, "frames": frames, "fps": 24})


async def validate_sources(audio: str | None, video: str | None, *, frames: int, width: int, height: int,
                           image: str | None = None) -> list[Source]:
    """Bound decoder admission; validate all sources before upload or quota."""
    if not audio and not video and not image:
        return []
    if not audio:
        raise HTTPException(422, detail="Video reference requires explicit exact-duration reference_audio")
    if sum(len(item) for item in (audio, video, image) if item) > MAX_ENCODED_BYTES:
        raise HTTPException(413, detail="Combined video conditioning inputs exceed their encoded limit")
    sources = [validate_image(decode_inline(image, "reference_image"))] if image else []
    sources.append(validate_audio(decode_inline(audio, "reference_audio"), frames))
    if video:
        raw = decode_inline(video, "reference_video")
        try:
            await asyncio.wait_for(_VIDEO_CHECKS.acquire(), timeout=1)
        except TimeoutError:
            raise HTTPException(503, detail="Video source verification is busy; retry later") from None
        # A cancelled HTTP request must not release a slot while its bounded
        # decoder thread still runs. Its own completion owns the permit.
        async def verify():
            try:
                return await asyncio.to_thread(validate_video, raw, frames, width, height)
            finally:
                _VIDEO_CHECKS.release()
        task = asyncio.create_task(verify())
        try:
            sources.append(await asyncio.shield(task))
        except asyncio.CancelledError:
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise
    return sources


async def upload_sources(sources: list[Source]) -> list[dict]:
    return [{**source.commitment, "url": await asyncio.to_thread(storage.upload_private_source, source.data, source.ext)}
            for source in sources]


def validate_commitments(sources: list[dict], bindings: list[dict], execution: dict) -> None:
    """Reject dropped, extra or mistimed source roles before reserving money."""
    expected = [{"role": "reference_audio", "path": "source_audio.inputs.audio"}]
    if execution.get("appearance_profile") == "ingredients-dev-v1":
        expected.insert(0, {"role": "reference_image", "path": "reference_image.inputs.image"})
    if execution.get("source_mode") == "video":
        expected.append({"role": "reference_video", "path": "source_video.inputs.file"})
    try:
        if (execution.get("source_mode") not in {"audio", "video"}
                or execution.get("appearance_profile") not in {None, "ingredients-dev-v1"} or bindings != expected
                or not isinstance(sources, list) or len(sources) != len(expected)):
            raise ValueError
        for source, binding in zip(sources, bindings):
            role = binding["role"]
            details = ({"width": 768, "height": 448} if role == "reference_image"
                       else {"sample_rate": 48_000, "channels": 2, "samples": execution["frames"] * 2000}
                       if role == "reference_audio" else {"width": execution["width"], "height": execution["height"]})
            mime, limit = (("image/png", MAX_IMAGE_BYTES) if role == "reference_image"
                           else ("audio/wav", MAX_AUDIO_BYTES) if role == "reference_audio" else ("video/mp4", MAX_VIDEO_BYTES))
            units = details if role == "reference_image" else {"frames": execution["frames"], "fps": 24, **details}
            expected_keys = {"role", "sha256", "bytes", "content_type", "url", *units}
            if not isinstance(source, dict) or set(source) != expected_keys:
                raise ValueError
            url = urlsplit(source["url"])
            if (not isinstance(source["url"], str) or len(source["url"]) > 4096
                    or url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.fragment or url.port not in {None, 443}
                    or source["role"] != role or source["content_type"] != mime
                    or not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                    or type(source["bytes"]) is not int or not 0 < source["bytes"] <= limit
                    or any(type(source[key]) is not int or source[key] != value
                           for key, value in units.items())):
                raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(422, detail="Source media does not match the governed video execution contract") from None
