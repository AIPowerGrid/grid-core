# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""/v1/styles — curated creative presets shared across all surfaces.

A style composes over a recipe (prompt template + LoRAs + good params + which
params are locked). Generation endpoints accept `style: "<id>"` and expand it
server-side; this router just lets clients discover what's available.
"""

import logging
from typing import Optional

from fastapi import APIRouter

from ..services import styles as styles_svc

logger = logging.getLogger("grid_api.styles")

router = APIRouter()


@router.get("/v1/styles")
async def list_styles(job_type: Optional[str] = None):
    """List curated styles, optionally filtered by job_type (image|video)."""
    return {"styles": styles_svc.list_styles(job_type=job_type)}
