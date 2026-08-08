"""Types shared by the service manager and the supervisors it drives.

Split out of :mod:`core.platform.service_manager` so a supervisor module can
describe its results without importing the manager — which in turn dispatches
into every supervisor. Keeping the types here (a module that imports nothing
from ``core.platform``) breaks what would otherwise be a
manager <-> supervisor import cycle. Same shape as
:mod:`core.federation.contract`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class ServiceSpec:
    """A managed service. ``container is None`` means host-native."""

    name: str
    container: Optional[str]
    compose_service: Optional[str]
    profile: Optional[str] = None
    startable: bool = True
    stoppable: bool = True
    # The app can't boot without these (schema init hard-fails if postgres is
    # down), so they always autostart and can't be removed from the list — see
    # REQUIRED_SERVICES and core/platform/autostart_config.py. Note this is
    # distinct from stoppable=False: ollama is non-stoppable but optional.
    required: bool = False
    description: str = ""


@dataclass
class ServiceStatus:
    name: str
    kind: str
    running: bool
    status: str
    installed: bool = True
    ready: bool = False
    managed_by_vigil: bool = False
    startable: bool = True
    stoppable: bool = True
    required: bool = False
    description: str = ""
    detail: Optional[str] = None


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    already_running: bool = False
    code: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)


class UnknownServiceError(KeyError):
    """Raised when a name isn't in the SERVICES allowlist."""
