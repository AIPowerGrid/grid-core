# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from scripts import alert_ops

NOW = datetime(2026, 12, 10, 5, tzinfo=UTC)
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abc_DEF-123"


def _policies(until: datetime) -> str:
    return json.dumps([
        {"since": "2026-12-01T05:00:00+00:00", "until": "2026-12-08T05:00:00+00:00",
         "price_micro_per_aipg": 1200, "worker_share_bps": 8500},
        {"since": "2026-12-08T05:00:00+00:00", "until": until.isoformat(),
         "price_micro_per_aipg": 1200, "worker_share_bps": 8500},
    ])


def test_expiry_is_silent_until_the_last_week():
    assert alert_ops.expiry_message(_policies(NOW + timedelta(days=7, hours=1)), NOW) is None
    assert alert_ops.expiry_message(None, NOW) is None
    assert alert_ops.expiry_message("[]", NOW) is None


def test_expiry_warns_with_the_end_time_and_after_it_passes():
    soon = alert_ops.expiry_message(_policies(NOW + timedelta(hours=30)), NOW)
    assert "2026-12-11 11:00 UTC" in soon and "about 30 hours" in soon
    ended = alert_ops.expiry_message(_policies(NOW - timedelta(hours=2)), NOW)
    assert "failing closed" in ended


def test_failure_message_only_names_aipg_units():
    assert "aipg-payout.service failed" in alert_ops.failure_message("aipg-payout.service")
    for bad in ("ssh.service", "aipg-payout.service; rm -rf /", "aipg-payout.timer", ""):
        with pytest.raises(ValueError):
            alert_ops.failure_message(bad)


def test_send_rejects_anything_but_a_discord_webhook():
    for bad in ("http://discord.com/api/webhooks/1/a", "https://example.com/api/webhooks/1/a",
                "https://discord.com.evil.test/api/webhooks/1/a"):
        with pytest.raises(ValueError):
            alert_ops.send(bad, "t", "m")


def test_failure_alert_is_sent_once_then_throttled(monkeypatch, tmp_path, capsys):
    sent = []
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("GRID_ALERT_DISCORD_WEBHOOK", WEBHOOK)
    monkeypatch.setattr(alert_ops, "send", lambda webhook, title, message: sent.append((title, message)))

    assert alert_ops.main(["unit-failed", "aipg-payout.service"]) == 0
    assert alert_ops.main(["unit-failed", "aipg-payout.service"]) == 0
    assert len(sent) == 1 and "aipg-payout.service" in sent[0][1]

    stamp = tmp_path / "failed-aipg-payout.service"
    old = stamp.stat().st_mtime - alert_ops.FAILURE_REPEAT_SECONDS - 1
    os.utime(stamp, (old, old))
    assert alert_ops.main(["unit-failed", "aipg-payout.service"]) == 0
    assert len(sent) == 2
    assert WEBHOOK not in capsys.readouterr().err


def test_never_fails_the_calling_unit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("GRID_ALERT_DISCORD_WEBHOOK", raising=False)
    assert alert_ops.main(["unit-failed", "aipg-payout.service"]) == 0
    assert "no webhook configured" in capsys.readouterr().err

    monkeypatch.setenv("GRID_ALERT_DISCORD_WEBHOOK", WEBHOOK)
    monkeypatch.setenv("WORKER_REWARD_DEMAND_POLICIES", "{not json")
    assert alert_ops.main(["policy-expiry"]) == 0

    def boom(*_args):
        raise OSError("network down")

    monkeypatch.setattr(alert_ops, "send", boom)
    assert alert_ops.main(["unit-failed", "aipg-payout.service"]) == 0
    err = capsys.readouterr().err
    assert "alert not sent" in err and WEBHOOK not in err
    assert not (tmp_path / "failed-aipg-payout.service").exists()
