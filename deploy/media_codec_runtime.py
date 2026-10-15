# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 AI Power Grid
"""Build or verify an offline, versioned codec candidate; never install/select it."""

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

SCHEMA = "aipg.media-codec-runtime.v1"
LOCK = Path(__file__).with_name("media-codecs.lock.json")
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024


def run(args, *, cwd=None, timeout=120, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, timeout=timeout, capture_output=True).stdout


def stream_digest(stream):
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(chunk)
    return value.hexdigest()


def digest(path):
    with path.open("rb") as stream:
        return stream_digest(stream)


def name_ok(name):
    return (
        isinstance(name, str)
        and 0 < len(name) <= 240
        and "\\" not in name
        and not PurePosixPath(name).is_absolute()
        and all(part not in {"", ".", ".."} for part in name.split("/"))
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("Codec source redirects are not allowed")


def download(url, target, limit):
    if not url.startswith("https://ffmpeg.org/"):
        raise ValueError("Unexpected codec source authority")
    with urllib.request.build_opener(NoRedirect).open(url, timeout=60) as response:
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("Codec source exceeds its bound")
    target.write_bytes(data)


def verify_source(archive, signature, key, directory, lock):
    if digest(archive) != lock["sha256"]:
        raise ValueError("Codec source hash mismatch")
    keyring = directory / "release-key.gpg"
    run(["gpg", "--batch", "--no-options", "--dearmor", "--output", str(keyring), str(key)])
    status = run(["gpgv", "--homedir", str(directory), "--keyring", str(keyring), "--status-fd", "1", str(signature), str(archive)]).decode(
        "ascii",
    )
    valid = [line.split()[2] for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if valid != [lock["signing_fingerprint"]]:
        raise ValueError("Codec signature does not match the pinned release authority")


def extract_source(archive, directory, version):
    prefix = "ffmpeg-" + version
    with tarfile.open(archive, "r:xz") as reader:
        members = reader.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > MAX_UNPACKED:
            raise ValueError("Codec source archive exceeds its bound")
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            if not name_ok(name) or name.split("/")[0] != prefix or name in seen or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe codec source archive")
            seen.add(name)
        for member in members:
            path = directory / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with reader.extractfile(member) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    return directory / prefix


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Unexpected codec bundle entry")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "size": path.stat().st_size,
                "sha256": digest(path),
                "mode": path.stat().st_mode & 0o777,
            }
    return result


def freeze_modes(root):
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Codec packaging cannot normalize a symlink")
        if path.is_file():
            path.chmod(0o755 if path.relative_to(root).parts[0] == "bin" else 0o644)


def _verify_reader(reader, expected_commit):
    members = []
    for member in reader:
        if len(members) >= 100:
            raise ValueError("Codec archive has too many entries")
        members.append(member)
    names = [m.name for m in members]
    if (
        len(names) != len(set(names))
        or not 6 <= len(names) <= 100
        or any(not name_ok(m.name) or not m.isfile() or m.size < 0 for m in members)
        or sum(m.size for m in members) > MAX_UNPACKED
        or "manifest.json" not in names
    ):
        raise ValueError("Unsafe codec runtime archive")
    manifest_member = reader.getmember("manifest.json")
    if manifest_member.size > 128 * 1024 or manifest_member.mode != 0o644:
        raise ValueError("Codec manifest exceeds its bound or has unsafe permissions")
    manifest = json.load(reader.extractfile(manifest_member))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SCHEMA
        or manifest.get("commit") != expected_commit
        or manifest.get("platform") != "linux-x86_64"
        or manifest.get("source") != json.loads(LOCK.read_text())
        or not isinstance(manifest.get("files"), dict)
        or "manifest.json" in manifest["files"]
        or set(names) != {*manifest["files"], "manifest.json"}
        or not {"bin/ffmpeg", "bin/ffprobe", "sbom.cdx.json", "source/build.py", "source/media-codecs.lock.json"} <= set(manifest["files"])
    ):
        raise ValueError("Codec manifest identity mismatch")
    for member in members:
        if member.name == "manifest.json":
            continue
        entry = manifest["files"][member.name]
        if (
            not isinstance(entry, dict)
            or type(entry.get("size")) is not int
            or entry["size"] != member.size
            or entry.get("mode") != (0o755 if member.name.startswith("bin/") else 0o644)
            or member.mode != entry["mode"]
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
        ):
            raise ValueError("Invalid codec file commitment: " + member.name)
        with reader.extractfile(member) as stream:
            if stream_digest(stream) != entry["sha256"]:
                raise ValueError("Codec runtime hash mismatch")
    return manifest


@contextmanager
def runtime_reader(archive):
    # Bound decompression before tar parses extended headers or follows offsets.
    with gzip.open(archive, "rb") as compressed, tempfile.TemporaryFile() as raw:
        total = 0
        while chunk := compressed.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UNPACKED + 1024 * 1024:
                raise ValueError("Codec archive expansion exceeds its bound")
            raw.write(chunk)
        raw.seek(0)
        with tarfile.open(fileobj=raw, mode="r:") as reader:
            yield reader


def verify_archive(archive, expected_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit) or archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Invalid codec archive or source commit")
    with runtime_reader(archive) as reader:
        return _verify_reader(reader, expected_commit)


def extract_verified(archive, destination, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Invalid codec archive or source commit")
    with runtime_reader(archive) as reader:
        manifest = _verify_reader(reader, commit)
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        for member in reader.getmembers():
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with reader.extractfile(member) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)
            path.chmod(member.mode)
            if member.name != "manifest.json" and digest(path) != manifest["files"][member.name]["sha256"]:
                raise ValueError("Codec archive changed during extraction; reject the incomplete stage")
    return manifest


def build(output):
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
        raise ValueError("Build codecs only on an unprivileged Linux x86_64 CI runner")
    root = Path(__file__).resolve().parents[1]
    if run(["git", "status", "--porcelain"], cwd=root).strip():
        raise ValueError("Codec builds require clean committed source")
    commit = run(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    lock = json.loads(LOCK.read_text())
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="aipg-codec-build-") as temporary:
        work = Path(temporary)
        package = work / "package"
        sources = package / "source"
        sources.mkdir(parents=True)
        for field, filename, limit in (
            ("archive", "ffmpeg.tar.xz", 64 * 1024 * 1024),
            ("signature", "ffmpeg.tar.xz.asc", 65536),
            ("key", "ffmpeg-devel.asc", 65536),
        ):
            download(lock[field], sources / filename, limit)
        verify_source(sources / "ffmpeg.tar.xz", sources / "ffmpeg.tar.xz.asc", sources / "ffmpeg-devel.asc", work, lock)
        source = extract_source(sources / "ffmpeg.tar.xz", work, lock["version"])
        shutil.copy2(__file__, sources / "build.py")
        shutil.copy2(LOCK, sources / "media-codecs.lock.json")
        dependencies = {}
        for binary in ("libx264-dev", "libc6-dev", "zlib1g-dev"):
            identity = run(["dpkg-query", "-W", "-f=${source:Package}=${source:Version}", binary]).decode().strip()
            dependencies[binary] = identity
            run(["apt-get", "source", "--download-only", identity], cwd=sources, timeout=300)
        libgcc = run(["cc", "-print-libgcc-file-name"]).decode().strip()
        gcc_package = run(["dpkg-query", "-S", libgcc]).decode().split(": ", 1)[0]
        dependencies[gcc_package] = run(["dpkg-query", "-W", "-f=${source:Package}=${source:Version}", gcc_package]).decode().strip()
        shutil.copy2(Path("/usr/share/doc") / gcc_package.split(":", 1)[0] / "copyright", sources / "gcc-runtime-copyright")
        options = [
            "--disable-autodetect",
            "--disable-network",
            "--disable-doc",
            "--disable-debug",
            "--disable-shared",
            "--enable-static",
            "--enable-gpl",
            "--enable-libx264",
            "--enable-zlib",
            "--disable-ffplay",
            "--disable-devices",
            "--enable-indev=lavfi",
            "--disable-hwaccels",
            "--disable-encoders",
            "--enable-encoder=libx264,aac,pcm_s16le,rawvideo,wrapped_avframe",
            "--disable-muxers",
            "--enable-muxer=mp4,wav,framemd5,null",
            "--extra-cflags=-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -march=x86-64 -mtune=generic",
            "--extra-ldexeflags=-static",
            "--extra-ldflags=-Wl,-z,relro,-z,now",
            "--pkg-config-flags=--static",
        ]
        # No app environment or credentials are needed by compiler/tool processes.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "LC_ALL": "C", "SOURCE_DATE_EPOCH": "1789699562"}
        try:
            run(["./configure", *options], cwd=source, env=env, timeout=120)
            run(["make", "-j2", "ffmpeg", "ffprobe"], cwd=source, env=env, timeout=1200)
        except subprocess.CalledProcessError as error:
            # Only authenticated public upstream/compiler diagnostics; never uploads.
            print(error.stderr.decode(errors="replace")[-12000:])
            raise
        (package / "bin").mkdir()
        versions = {}
        for tool in ("ffmpeg", "ffprobe"):
            binary = source / tool
            if b"INTERP" in run(["readelf", "-l", str(binary)]) or b"NEEDED" in run(["readelf", "-d", str(binary)]):
                raise ValueError("Codec runtime unexpectedly requires host shared libraries")
            versions[tool] = run([str(binary), "-version"], env=env).decode().splitlines()[0]
            if not versions[tool].startswith(tool + " version " + lock["version"] + " "):
                raise ValueError("Codec executable version mismatch")
            shutil.copy2(binary, package / "bin" / tool)
            (package / "bin" / tool).chmod(0o755)
        components = [
            {
                "type": "application",
                "name": "ffmpeg",
                "version": lock["version"],
                "hashes": [{"alg": "SHA-256", "content": lock["sha256"]}],
            },
        ]
        components += [
            {"type": "library", "name": value.split("=", 1)[0], "version": value.split("=", 1)[1]} for value in dependencies.values()
        ]
        (package / "sbom.cdx.json").write_text(
            json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "components": components}, indent=2) + "\n",
        )
        freeze_modes(package)
        manifest = {
            "schema": SCHEMA,
            "commit": commit,
            "platform": "linux-x86_64",
            "source": lock,
            "versions": versions,
            "configure": options,
            "dependencies": dependencies,
            "compiler": run(["cc", "--version"]).decode().splitlines()[0],
            "files": inventory(package),
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        (package / "manifest.json").chmod(0o644)
        archive = output / ("media-codecs-" + commit + ".tar.gz")
        with tarfile.open(archive, "w:gz") as writer:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    writer.add(path, arcname=path.relative_to(package).as_posix(), recursive=False)
        verify_archive(archive, commit)
        shutil.copy2(package / "manifest.json", output / "manifest.json")
        shutil.copy2(package / "sbom.cdx.json", output / "sbom.cdx.json")
        (output / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.name}\n" for p in sorted(output.iterdir()) if p.is_file()))
        print(json.dumps({"archive": archive.name, "sha256": digest(archive), "commit": commit}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--extract", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.output)
    else:
        result = extract_verified(args.archive, args.extract, args.commit) if args.extract else verify_archive(args.archive, args.commit)
        print(json.dumps({"schema": result["schema"], "commit": result["commit"], "verified": True}))


if __name__ == "__main__":
    main()
