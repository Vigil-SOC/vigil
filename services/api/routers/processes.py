"""GET /api/processes: whether the agent layer and the SOC daemon are running.

Read by setup's system checks and the console's status line. Signed-in only:
the answer says whether alerts are being polled, which an anonymous caller has
no need to learn. See core/platform/process_health.py for what each value means.

Parked here, beside local_services, rather than in core/platform: that tier may
not import core.deps, which reaches the capability domains.
"""

from fastapi import APIRouter, Depends

from core.deps import provide_process_health
from core.platform.process_health import ProcessHealth, ProcessStates
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/processes",
    tags=["processes"],
    auth=Auth.REQUIRED,
)


@router.get("")
async def get_processes(
    processes: ProcessHealth = Depends(provide_process_health),
) -> ProcessStates:
    """State of the agent layer and the SOC daemon.

    ``agent_layer`` is ``no_token`` when this server has no
    AGENT_INTERNAL_TOKEN. Up to five seconds old.
    """
    return await processes.read()
