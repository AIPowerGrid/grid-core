# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P networking module using libp2p.

This module provides decentralized job distribution via gossipsub,
replacing the centralized Redis-based job queue when P2P mode is enabled.

Usage:
    from grid_api.services.p2p import get_p2p_node, init_p2p, close_p2p

    # On startup
    await init_p2p()

    # Submit a job (broadcasts to gossipsub)
    from grid_api.services.p2p.job_queue import submit_job
    await submit_job(job_id, payload, models)

    # On shutdown
    await close_p2p()
"""

from .node import P2PNode, get_p2p_node, init_p2p, close_p2p
from .config import P2PConfig, get_p2p_config

__all__ = [
    "P2PNode",
    "get_p2p_node",
    "init_p2p",
    "close_p2p",
    "P2PConfig",
    "get_p2p_config",
]
