# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import stat
import sys

import pytest

from scripts import manage_validator_recipient as cli


def arguments(monkeypatch, output, *extra, action="bind"):
    monkeypatch.setattr(sys, "argv", ["manage_validator_recipient.py", action, "--input", "private.json", "--output", str(output), *extra])


def test_stdout_is_redacted_output_private_and_default_readonly(monkeypatch, tmp_path, capsys):
    output = tmp_path / "request.json"
    arguments(monkeypatch, output, action="prepare")

    async def run(args):
        assert not args.apply
        return {"sendable": False, "consent": {"recipient": "private-wallet"}, "message": "private-signing-request"}

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"sendable": False}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["consent"]["recipient"] == "private-wallet"


@pytest.mark.parametrize("action,extra", [("prepare", ["--apply", "--expect-digest", "a" * 64]), ("bind", ["--apply"])])
def test_missing_approval_or_mutating_prepare_rejected(monkeypatch, tmp_path, action, extra):
    arguments(monkeypatch, tmp_path / "out.json", *extra, action=action)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_existing_output_prevents_any_database_action(monkeypatch, tmp_path):
    output = tmp_path / "out.json"
    output.write_text("keep")
    arguments(monkeypatch, output)

    async def run(args):
        pytest.fail("must not touch database")

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 1 and output.read_text() == "keep"


def test_failure_hides_exception_and_explains_same_input_retry(monkeypatch, tmp_path, capsys):
    arguments(monkeypatch, tmp_path / "out.json")

    async def run(args):
        raise RuntimeError("private credential in DB error")

    monkeypatch.setattr(cli, "run", run)
    assert cli.main() == 1
    message = capsys.readouterr().err
    assert "private credential" not in message
    assert "may have committed" in message and "same signed input" in message
