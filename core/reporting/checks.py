"""The one Report Check registry.

Each check has Vigil's own id, its module, the permission that gates its
source, and its rule. A value check also carries the cited value, the source
id, the clause, the link and the date it was checked. There is no field for
quoted text. Outcomes are not stored here; they live on the Report Record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReportModule:
    id: str
    permission: str


@dataclass(frozen=True)
class ReportCheck:
    id: str
    module: str
    permission: str
    rule: str
    cited_value: Optional[str] = None
    source_id: Optional[str] = None
    clause: Optional[str] = None
    link: Optional[str] = None
    date_checked: Optional[str] = None


VISIBILITY = ReportModule("visibility", "integrations.read")

# Value-check fields stay on the entry for later modules. None are set here.
CHECKS: tuple[ReportCheck, ...] = (
    ReportCheck(
        "visibility.daemon",
        VISIBILITY.id,
        VISIBILITY.permission,
        "The background service is running.",
    ),
    ReportCheck(
        "visibility.model",
        VISIBILITY.id,
        VISIBILITY.permission,
        "A model is assigned and its provider is routable.",
    ),
    ReportCheck(
        "visibility.collection",
        VISIBILITY.id,
        VISIBILITY.permission,
        "At least one alert source is switched on, and every alert source is collected by scheduled collection or an enabled receiver.",
    ),
    ReportCheck(
        "visibility.source",
        VISIBILITY.id,
        VISIBILITY.permission,
        "A switched-on source is healthy. Last data and webhook labels are information.",
    ),
)

MODULES = {VISIBILITY.id: VISIBILITY}


def spec_for(check_id: str) -> ReportCheck:
    """The registry entry a produced check id belongs to."""
    for spec in CHECKS:
        if check_id == spec.id or check_id.startswith(spec.id + "."):
            return spec
    raise KeyError(check_id)
