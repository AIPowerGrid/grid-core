# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail-closed compatibility shim for retired coordinator probes.

The old sampler bypassed reservations through ordinary worker dispatch. Public
validators and worker setup use separate, bound, economically inert paths.
"""

import logging

logger = logging.getLogger("grid_api.probe")
_RETIRED = "Coordinator probes are retired; use assignment-bound validator probes."


async def _run_job(model: str, prompt: str):
    raise RuntimeError(_RETIRED)


async def run_once():
    raise RuntimeError(_RETIRED)


async def probe_loop() -> None:
    logger.warning(_RETIRED)
