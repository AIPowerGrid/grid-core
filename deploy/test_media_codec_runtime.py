# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 AI Power Grid
"""Codec bundle negative controls use only disposable, non-executable bytes."""

import copy
import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_codec_runtime import LOCK, SCHEMA, extract_source, extract_verified, name_ok, verify_archive, verify_source

COMMIT = "1" * 40


class CodecArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "runtime.tar.gz"
        self.files = {
            name: b"non-executable codec fixture"
            for name in (
                "bin/ffmpeg",
                "bin/ffprobe",
                "sbom.cdx.json",
                "source/build.py",
                "source/media-codecs.lock.json",
            )
        }
        self.manifest = {
            "schema": SCHEMA,
            "commit": COMMIT,
            "platform": "linux-x86_64",
            "source": json.loads(LOCK.read_text()),
            "files": {
                name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o755 if name.startswith("bin/") else 0o644}
                for name, data in self.files.items()
            },
        }

    def archive_for(self, *, change=None, body_change=None, extra=None, duplicate=False):
        manifest = copy.deepcopy(self.manifest)
        if change:
            change(manifest)
        files = dict(self.files)
        if body_change:
            body_change(files)
        files["manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(self.archive, "w:gz") as writer:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.mode = manifest["files"].get(name, {}).get("mode", 0o644)
                member.size = len(data)
                writer.addfile(member, io.BytesIO(data))
                if duplicate and name == "manifest.json":
                    writer.addfile(member, io.BytesIO(data))
            if extra:
                writer.addfile(extra, io.BytesIO(b"x") if extra.isfile() else None)

    def test_valid_bundle_verifies_and_extracts_once(self):
        self.archive_for()
        self.assertEqual(verify_archive(self.archive, COMMIT)["commit"], COMMIT)
        stage = self.root / ".stage"
        extract_verified(self.archive, stage, COMMIT)
        self.assertEqual((stage / "bin/ffprobe").read_bytes(), self.files["bin/ffprobe"])
        self.assertEqual((stage / "bin/ffprobe").stat().st_mode & 0o777, 0o755)
        with self.assertRaises(FileExistsError):
            extract_verified(self.archive, stage, COMMIT)

    def test_identity_and_commitment_tampering_reject_before_writing(self):
        changes = [
            lambda m: m.update(commit="2" * 40),
            lambda m: m.update(schema="wrong"),
            lambda m: m.update(platform="darwin-arm64"),
            lambda m: m["source"].update(version="old"),
            lambda m: m["files"]["bin/ffmpeg"].update(size=0),
            lambda m: m["files"]["bin/ffmpeg"].update(mode=0o4755),
            lambda m: m["files"]["bin/ffmpeg"].update(sha256="0" * 64),
            lambda m: m["files"].pop("bin/ffmpeg"),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.archive_for(change=change)
                stage = self.root / ("stage-" + str(index))
                with self.assertRaises(ValueError):
                    extract_verified(self.archive, stage, COMMIT)
                self.assertFalse(stage.exists())

    def test_changed_bytes_duplicate_and_uncommitted_entries_fail(self):
        self.archive_for(body_change=lambda f: f.update({"bin/ffmpeg": b"different"}))
        with self.assertRaises(ValueError):
            verify_archive(self.archive, COMMIT)
        self.archive_for(duplicate=True)
        with self.assertRaises(ValueError):
            verify_archive(self.archive, COMMIT)
        for name in ("../escape", "/absolute", "source/../../escape", "extra", "bin\\escape"):
            member = tarfile.TarInfo(name)
            member.size = 1
            self.archive_for(extra=member)
            with self.assertRaises(ValueError):
                verify_archive(self.archive, COMMIT)

    def test_links_and_special_entries_are_never_extracted(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            member = tarfile.TarInfo("source/link")
            member.type, member.linkname = kind, "/etc/shadow"
            self.archive_for(extra=member)
            with self.assertRaises(ValueError):
                verify_archive(self.archive, COMMIT)

    def test_source_hash_and_signing_authority_are_independent_gates(self):
        archive = self.root / "source.xz"
        archive.write_bytes(b"fixture source")
        lock = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "signing_fingerprint": "A" * 40}
        with patch("media_codec_runtime.run", side_effect=[b"", b"[GNUPG:] VALIDSIG " + b"B" * 40 + b" ignored"]):
            with self.assertRaisesRegex(ValueError, "authority"):
                verify_source(archive, self.root / "sig", self.root / "key", self.root, lock)
        lock["sha256"] = "0" * 64
        with patch("media_codec_runtime.run") as command:
            with self.assertRaisesRegex(ValueError, "hash"):
                verify_source(archive, self.root / "sig", self.root / "key", self.root, lock)
            command.assert_not_called()

    def test_source_extraction_rejects_links_before_writes(self):
        source = self.root / "source.tar.xz"
        with tarfile.open(source, "w:xz") as writer:
            member = tarfile.TarInfo("ffmpeg-9.0.2/unsafe")
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/shadow"
            writer.addfile(member)
        with self.assertRaises(ValueError):
            extract_source(source, self.root, "9.0.2")
        self.assertFalse((self.root / "ffmpeg-9.0.2").exists())

    def test_unsafe_names(self):
        for name in ("", ".", "a//b", "a/./b", "../x", "/x", "a\\b"):
            self.assertFalse(name_ok(name))

    def test_expansion_is_bounded_before_parsing_tar(self):
        with gzip.open(self.archive, "wb") as writer:
            writer.write(b"\0" * (2 * 1024 * 1024))
        with patch("media_codec_runtime.MAX_UNPACKED", 64):
            with self.assertRaisesRegex(ValueError, "expansion"):
                verify_archive(self.archive, COMMIT)


if __name__ == "__main__":
    unittest.main()
