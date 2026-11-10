#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tell the operator when the payout sender fails or is about to stop.

Two modes, both run by systemd next to `aipg-payout.service`:

  alert_ops.py unit-failed <unit>   OnFailure hook: the unit just failed.
  alert_ops.py policy-expiry        After a good run: warn when the reviewed
                                    reward-policy windows end soon.
  alert_ops.py aipg-price-expiry    After a good run: warn when the published
                                    AIPG funding price is about to lapse.

Standard library only, no database and no chain access. Alerts carry the unit
name or a date, never logs, amounts, addresses or configuration. A missing
webhook or a delivery failure is reported on stderr and never fails the unit
that triggered it. Repeats are throttled through a stamp file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+$")
UNIT_RE = re.compile(r"^aipg-[a-z0-9-]{1,48}\.service$")
FAILURE_REPEAT_SECONDS = 6 * 3600
EXPIRY_REPEAT_SECONDS = 24 * 3600
EXPIRY_WARNING_SECONDS = 7 * 86400
AIPG_PRICE_WARNING_SECONDS = 48 * 3600
AIPG_PRICE_REPEAT_SECONDS = 12 * 3600


def policy_end(raw: str | None) -> datetime | None:
    """End of the last reward-policy window, or None when none is configured."""
    if not raw or not raw.strip():
        return None
    policies = json.loads(raw)
    if not isinstance(policies, list) or not policies:
        return None
    end = datetime.fromisoformat(str(policies[-1]["until"]))
    if end.tzinfo is None:
        raise ValueError("reward policy window has no timezone")
    return end.astimezone(UTC)


def expiry_message(raw: str | None, now: datetime) -> str | None:
    """Warning text when the configured windows end within the warning period."""
    end = policy_end(raw)
    if end is None:
        return None
    remaining = (end - now).total_seconds()
    if remaining > EXPIRY_WARNING_SECONDS:
        return None
    when = end.strftime("%Y-%m-%d %H:%M UTC")
    if remaining <= 0:
        return f"Worker reward policy windows ended {when}. Hourly payouts are failing closed until a window is appended."
    hours = int(remaining // 3600)
    return (
        f"Worker reward policy windows end {when} (about {hours} hours). "
        "Hourly payouts will fail closed after that unless a reviewed window is appended."
    )


def aipg_price_message(path: str | None, now: datetime) -> str | None:
    """Warning text when the AIPG funding price file is missing, unreadable or
    within two days of expiry. None when no price file is configured."""
    if not path or not path.strip():
        return None
    try:
        end = datetime.fromisoformat(str(json.loads(Path(path.strip()).read_text())["valid_until"]))
        if end.tzinfo is None:
            raise ValueError("price has no timezone")
    except (OSError, ValueError, KeyError, TypeError):
        return "The AIPG funding price file is missing or unreadable. AIPG deposits are unavailable until a price is published."
    remaining = (end.astimezone(UTC) - now).total_seconds()
    if remaining > AIPG_PRICE_WARNING_SECONDS:
        return None
    when = end.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if remaining <= 0:
        return f"The AIPG funding price expired {when}. AIPG deposits are unavailable until a new price is published."
    return (
        f"The AIPG funding price expires {when} (about {int(remaining // 3600)} hours). "
        "Publish a new one with scripts/set_aipg_price.py or AIPG deposits will stop."
    )


def failure_message(unit: str) -> str:
    if not UNIT_RE.fullmatch(unit):
        raise ValueError("unit name is not an AIPG service")
    return f"{unit} failed. Check `journalctl -u {unit}` on the Core host."


def due(stamp: Path, repeat_seconds: int, now: float) -> bool:
    try:
        return now - stamp.stat().st_mtime >= repeat_seconds
    except FileNotFoundError:
        return True


def send(webhook: str, title: str, message: str, timeout: float = 10.0) -> None:
    if not WEBHOOK_RE.fullmatch(webhook):
        raise ValueError("alert webhook is not a Discord webhook URL")
    body = json.dumps({
        "embeds": [{"title": title, "description": message, "color": 0xE5484D}],
        "allowed_mentions": {"parse": []},
    }).encode()
    request = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "aipg-ops-alert/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - host pinned by WEBHOOK_RE
        response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    failed = modes.add_parser("unit-failed")
    failed.add_argument("unit")
    modes.add_parser("policy-expiry")
    modes.add_parser("aipg-price-expiry")
    args = parser.parse_args(argv)

    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/aipg-ops-alert").split(":")[0])
    try:
        if args.mode == "unit-failed":
            title, message = "Payout sender failed", failure_message(args.unit)
            stamp, repeat = state / f"failed-{args.unit}", FAILURE_REPEAT_SECONDS
        elif args.mode == "aipg-price-expiry":
            title = "AIPG funding price expiring"
            message = aipg_price_message(os.environ.get("GRID_AIPG_PRICE_FILE"), datetime.now(UTC))
            stamp, repeat = state / "aipg-price-expiry", AIPG_PRICE_REPEAT_SECONDS
        else:
            title = "Reward policy windows ending"
            message = expiry_message(os.environ.get("WORKER_REWARD_DEMAND_POLICIES"), datetime.now(UTC))
            stamp, repeat = state / "policy-expiry", EXPIRY_REPEAT_SECONDS
        if message is None or not due(stamp, repeat, time.time()):
            return 0
        webhook = os.environ.get("GRID_ALERT_DISCORD_WEBHOOK", "").strip()
        if not webhook:
            print(f"alert not sent (no webhook configured): {title}", file=sys.stderr)
            return 0
        send(webhook, title, message)
        state.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        # Never fail the caller, and never echo configuration or a webhook URL.
        print(f"alert not sent ({type(exc).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
