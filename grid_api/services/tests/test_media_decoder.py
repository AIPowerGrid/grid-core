# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import subprocess
import sys

import pytest

from grid_api.services import media_decoder
from grid_api.services.tests.test_video_sources import mp4 as mp4


@pytest.mark.parametrize("overflow,diagnostic", [(False, False), (True, False), (False, True)])
def test_linux_command_is_confined_and_parent_reads_are_bounded(monkeypatch, tmp_path, overflow, diagnostic):
    monkeypatch.setattr(media_decoder, "available", lambda: True)
    monkeypatch.setattr(media_decoder.sys, "platform", "linux")
    monkeypatch.setattr(media_decoder.shutil, "which", lambda name: "/usr/bin/"+name)

    def run(command, **kwargs):
        assert command[0] == "prlimit" and "bwrap" in command
        assert "--unshare-all" in command and "--clearenv" in command
        assert "--cap-drop" in command and "--ro-bind" in command
        assert "/etc/ld.so.cache" in command and "/etc/alternatives" in command
        assert "/etc" not in command and "--share-net" not in command
        assert "OPENBLAS_NUM_THREADS" in command and "OMP_NUM_THREADS" in command
        assert "/work/source.mp4" in command and str(tmp_path/"source.mp4") not in command
        assert "capture_output" not in kwargs and kwargs["timeout"] == 10
        assert set(kwargs["env"]) == {"PATH"}
        kwargs["stdout"].write(b"x"*33 if overflow else b"ok")
        if diagnostic:
            kwargs["stderr"].write(b"fixture-private-path")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(media_decoder.subprocess, "run", run)
    if overflow or diagnostic:
        with pytest.raises(ValueError, match="invalid output"):
            media_decoder.run_tool("ffprobe", [str(tmp_path/"source.mp4")], tmp_path, timeout=10, limit=32)
    else:
        assert media_decoder.run_tool("ffprobe", [str(tmp_path/"source.mp4")], tmp_path, timeout=10, limit=32) == b"ok"


def test_missing_isolation_or_unknown_tool_cannot_fall_back(monkeypatch, tmp_path):
    monkeypatch.setattr(media_decoder, "available", lambda: False)
    for tool in ("ffmpeg", "ffprobe", "sh"):
        with pytest.raises(OSError, match="isolation is unavailable"):
            media_decoder.run_tool(tool, [], tmp_path, timeout=10, limit=32)


@pytest.mark.skipif(sys.platform != "linux", reason="Requires actual Linux source sandbox")
def test_real_linux_decoder_cannot_read_outside_its_workspace(tmp_path, mp4):
    if not media_decoder.available():
        pytest.fail("Linux source decoding requires FFmpeg, ffprobe, bubblewrap and prlimit")
    # Use the real existing fixture generator without requiring a GPU or Grid.
    source = mp4
    work = tmp_path/"work"
    work.mkdir()
    inside, outside = work/"source.mp4", tmp_path/"outside.mp4"
    inside.write_bytes(source)
    outside.write_bytes(source)
    args = ["-v", "error", "-f", "mov", "-show_entries", "format=format_name", "-of", "json"]
    assert media_decoder.run_tool("ffprobe", [*args, str(inside)], work, timeout=10, limit=32768)
    with pytest.raises((OSError, ValueError, subprocess.SubprocessError)):
        media_decoder.run_tool("ffprobe", [*args, str(outside)], work, timeout=10, limit=32768)


@pytest.mark.skipif(sys.platform != "linux", reason="Requires actual Linux source sandbox")
@pytest.mark.parametrize("utility,args", [
    ("env", []), ("readlink", ["/proc/self/ns/net"]), ("cat", ["/proc/self/status"]),
])
def test_real_linux_launcher_strips_secrets_network_and_capabilities(monkeypatch, tmp_path, utility, args):
    if not media_decoder.available():
        pytest.fail("Linux source decoding requires FFmpeg, ffprobe, bubblewrap and prlimit")
    # Exercise the actual production launcher with fixed trusted inspection
    # utilities. Application code still accepts only its two codec tool names.
    which = media_decoder.shutil.which
    monkeypatch.setattr(media_decoder.shutil, "which", lambda name: which(utility if name == "ffprobe" else name))
    monkeypatch.setenv("GRID_CODEC_FIXTURE_SECRET", "must-not-reach-codec")
    output = media_decoder.run_tool("ffprobe", args, tmp_path, timeout=10, limit=32768).decode()
    if utility == "env":
        assert "GRID_CODEC_FIXTURE_SECRET" not in output
        assert "OPENBLAS_NUM_THREADS=1" in output
    elif utility == "readlink":
        assert output.strip() != os.readlink("/proc/self/ns/net")
    else:
        assert "CapEff:\t0000000000000000\n" in output
