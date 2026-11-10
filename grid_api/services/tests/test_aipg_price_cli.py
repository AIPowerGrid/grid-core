# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest

from grid_api.services import deposits
from scripts import set_aipg_price

NOW = datetime(2026, 11, 10, 12, 30, 45, tzinfo=UTC)


def test_record_backdates_as_of_by_the_grace_period():
    record = set_aipg_price.build_record(
        price_micro=1000, valid_hours=168, grace_minutes=60, block=0, epoch=None, now=NOW,
    )
    assert record == {
        "version": 1, "epoch": "2026-11-10T1230Z", "price_micro": 1000,
        "as_of": "2026-11-10T11:30:45+00:00", "valid_until": "2026-11-17T12:30:45+00:00", "block": 0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price_micro": 0}, {"price_micro": 10**10}, {"valid_hours": 0.5}, {"valid_hours": 400},
        {"grace_minutes": -1}, {"grace_minutes": 5000}, {"block": -1}, {"epoch": "no spaces"},
    ],
)
def test_record_rejects_out_of_range_input(kwargs):
    base = {"price_micro": 1000, "valid_hours": 168, "grace_minutes": 60, "block": 0, "epoch": None, "now": NOW}
    with pytest.raises(ValueError):
        set_aipg_price.build_record(**{**base, **kwargs})


def test_dry_run_writes_nothing_and_apply_publishes_a_file_core_accepts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "aipg-price.json"
    assert set_aipg_price.main(["--price-micro", "1000", "--file", str(path)]) == 0
    assert not path.exists() and "dry run" in capsys.readouterr().out

    assert set_aipg_price.main(["--price-micro", "1000", "--file", str(path), "--apply"]) == 0
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o644
    assert set(json.loads(path.read_text())) == {"version", "epoch", "price_micro", "as_of", "valid_until", "block"}

    # The file the tool writes is one Core's reader accepts, for the whole week.
    monkeypatch.setattr(deposits, "AIPG_PRICE_FILE", str(path))
    monkeypatch.setattr(deposits, "AIPG_PRICE_MAX_AGE_SECONDS", 8 * 86400)
    assert deposits._aipg_price().price_micro == 1000
    last_hour = datetime.now(UTC) + timedelta(days=7) - timedelta(minutes=1)
    assert deposits._aipg_price(last_hour) is not None
    assert deposits._aipg_price(last_hour + timedelta(minutes=2)) is None


def test_large_price_change_needs_an_explicit_flag(tmp_path, capsys):
    path = tmp_path / "aipg-price.json"
    assert set_aipg_price.main(["--price-micro", "1000", "--file", str(path), "--apply"]) == 0
    assert set_aipg_price.main(["--price-micro", "1600", "--file", str(path), "--apply"]) == 2
    assert set_aipg_price.main(["--price-micro", "600", "--file", str(path), "--apply"]) == 2
    assert json.loads(path.read_text())["price_micro"] == 1000
    assert "more than 50%" in capsys.readouterr().err
    assert set_aipg_price.main(["--price-micro", "1400", "--file", str(path), "--apply"]) == 0
    assert set_aipg_price.main(
        ["--price-micro", "3000", "--file", str(path), "--apply", "--allow-large-change"]) == 0
    assert json.loads(path.read_text())["price_micro"] == 3000
