# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local conftest — kept minimal so these unit tests don't pull in the
top-level tests/conftest.py which expects a running Horde server."""

import sys
from pathlib import Path

# Ensure the repo root is importable so `from grid_api.services.settlement import ...`
# works when running pytest from any directory.
ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
