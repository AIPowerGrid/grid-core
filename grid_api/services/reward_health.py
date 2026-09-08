# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only warning for unbacked unrestricted reward eligibility."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..config import get_settings
from . import alerts
from .settlement.aggregate import reward_backing_health

logger = logging.getLogger("grid_api.reward_health")


async def check_and_alert() -> None:
    if not get_settings().grid_reward_monitor_enabled:
        return
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    try:
        async with asyncio.timeout(20):
            health = await reward_backing_health(start, end)
    except Exception as exc:
        logger.warning("Reward backing check unavailable error_type=%s", type(exc).__name__)
        alerts.emit(
            "reward_monitor_failed", "warning",
            "Reward backing is unknown because the read-only check failed.",
            fields={"error_type": type(exc).__name__},
        )
        return
    if health["unbacked_jobs"]:
        alerts.emit(
            "unbacked_reward_eligibility", "critical",
            "Completed work can enter the unrestricted reward pool without full purchased backing. "
            "This is eligibility exposure, not proof of a transfer. Review before resuming payouts.",
            fields={**health, "period_start": start.isoformat(), "period_end": end.isoformat()},
        )
