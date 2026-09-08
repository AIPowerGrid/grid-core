# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
import stat
import sys

import pytest

from scripts import manage_validator_compensation as cli


def args(monkeypatch, output, *extra):
    monkeypatch.setattr(
        sys,
        "argv",
        ["manage_validator_compensation.py", "create", "--input", "unused-private-request.json", "--output", str(output), *extra],
    )


def test_default_is_preview_and_stdout_does_not_publish_control_mapping(tmp_path, monkeypatch, capsys):
    output = tmp_path / "preview.json"
    args(monkeypatch, output)

    async def run(options):
        assert options.apply is False
        return {
            "digest": "a" * 64,
            "dry_run": True,
            "sendable": False,
            "contract": {"operator_group_id": "private-control-group", "account_id": "private-account"},
        }

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 0
    terminal = capsys.readouterr()
    assert "private-control-group" not in terminal.out and "private-account" not in terminal.out
    assert json.loads(terminal.out)["sendable"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["contract"]["account_id"] == "private-account"


def test_apply_requires_exact_preview_digest(tmp_path, monkeypatch):
    args(monkeypatch, tmp_path / "output.json", "--apply")
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_failed_operation_hides_exception_and_preserves_uncertain_commit_warning(tmp_path, monkeypatch, capsys):
    args(monkeypatch, tmp_path / "output.json")

    async def run(options):
        raise RuntimeError("credential-bearing SQL exception")

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 1
    terminal = capsys.readouterr()
    assert "credential-bearing" not in terminal.err
    assert "may have committed" in terminal.err and "same campaign" in terminal.err


@pytest.mark.skipif(os.name != "posix", reason="private operational CLI is POSIX-only")
def test_existing_output_is_not_overwritten_or_used_to_authorize_a_write(tmp_path, monkeypatch):
    output = tmp_path / "original.json"
    output.write_text("keep")
    args(monkeypatch, output, "--apply", "--expect-digest", "a" * 64)

    async def run(options):
        pytest.fail("must reject an existing output before database work")

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 1 and output.read_text() == "keep"
