"""GET /api/install-readiness: Install Readiness, computed when read.

Served under ``platform.read``. It blocks nothing. A store or probe that
cannot be read answers 200 with state ``unknown``, not a list of healthy
items. The desktop app answers the one line that it has no background service.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.auth.permissions import permission_gate
from core.deps import provide_process_health
from core.integrations.readiness import evaluate
from core.platform.process_health import ProcessHealth
from core.routing import Auth, RouterMeta

router = APIRouter(dependencies=[permission_gate("platform.read")])

ROUTER_META = RouterMeta(
    prefix="/api/install-readiness",
    tags=["install-readiness"],
    auth=Auth.REQUIRED,
)


class ReadinessItem(BaseModel):
    id: str
    text: str
    fix: str
    href: Optional[str] = None


class ReadinessNote(BaseModel):
    id: str
    text: str


class InstallReadiness(BaseModel):
    state: str = Field(
        description="ready, not_ready, unknown, or unavailable on the desktop app"
    )
    line: str
    items: List[ReadinessItem]
    notes: List[ReadinessNote]


@router.get("", response_model=InstallReadiness)
async def install_readiness(
    processes: ProcessHealth = Depends(provide_process_health),
) -> InstallReadiness:
    """Whether the install is ready, with each unmet item and its fix."""
    return InstallReadiness.model_validate(await evaluate(processes))
