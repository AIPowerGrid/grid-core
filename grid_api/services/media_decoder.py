# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded verification tools; Linux never decodes uploads in the app namespace."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def available() -> bool:
    tools = ("ffmpeg", "ffprobe", "bwrap", "prlimit") if sys.platform == "linux" else ("ffmpeg", "ffprobe")
    return all(shutil.which(tool) for tool in tools) and (
        sys.platform == "linux" or (sys.platform == "darwin" and os.geteuid() != 0)
    )


def run_tool(tool: str, args: list[str], directory: Path, *, timeout: int, limit: int) -> bytes:
    if tool not in {"ffmpeg", "ffprobe"} or not available() or not 0 < limit <= 65536:
        raise OSError("Media decoder isolation is unavailable")
    program = shutil.which(tool)
    assert program is not None
    command = [program, *args]
    if sys.platform == "linux":
        if not Path(program).is_absolute():
            raise OSError("Media decoder executable must be absolute")
        mapped = ["/work/" + Path(value).name if value.startswith(str(directory) + "/") else value for value in args]
        command = [
            "prlimit", "--as=1073741824", "--cpu=30", "--fsize=1048576", "--nofile=64", "--", "bwrap",
            "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
            "--ro-bind", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--ro-bind", str(directory), "/work", "--chdir", "/work", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
            "--ro-bind", program, "/codec/tool", "--", "/codec/tool", *mapped,
        ]
    # Child output goes to bounded-on-Linux files, never an unbounded parent
    # capture buffer. No tool diagnostics or private source paths escape.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
        subprocess.run(command, cwd=directory, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
                       stdout=output, stderr=diagnostic, timeout=timeout, check=True)
        output.seek(0)
        diagnostic.seek(0)
        data = output.read(limit + 1)
        if len(data) > limit or diagnostic.read(1):
            raise ValueError("Media decoder produced invalid output")
        return data
