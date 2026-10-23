"""The /api/v1 router. Every route requires the admin token.

Unmatched paths under the prefix are guarded too, in the 404/405 handler (``errors.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from jarvis_api.http import keys, projects
from jarvis_api.http.deps import API_PREFIX, require_admin

router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_admin)])
router.include_router(projects.router)
router.include_router(keys.router)
