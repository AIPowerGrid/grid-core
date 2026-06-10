# SPDX-License-Identifier: AGPL-3.0-or-later

"""Minimal conftest for grid_api unit tests — bypasses the top-level
tests/conftest.py that expects a running Horde server."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# hash_api_key fails hard without GRID_SALT (by design); give tests one.
os.environ.setdefault("GRID_SALT", "test-salt-not-for-production")
