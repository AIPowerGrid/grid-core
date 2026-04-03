#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Entry point for the Grid Streaming API server.

Run:  python server_grid_api.py
Or:   uvicorn horde.grid_api.main:app --host 0.0.0.0 --port 7002
"""

import uvicorn

from grid_api.config import get_settings


def main():
    settings = get_settings()
    uvicorn.run(
        "grid_api.main:app",
        host=settings.grid_api_host,
        port=settings.grid_api_port,
        workers=4,
        log_level="info",
    )


if __name__ == "__main__":
    main()
