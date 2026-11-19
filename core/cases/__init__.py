"""Cases domain package (Reorg R5 / #486).

Case lifecycle — evidence, IOCs, SLA, metrics, notifications,
collaboration, search, automation, workflow — plus sandbox-report correlation
into case evidence.

Callers import directly from ``core.cases.<module>``; the legacy
``services.*`` paths were removed with this move.
"""

# Installs the case row's trail writer for core.storage (case_trail_hook),
# and who a removal names when it sends an alert back to intake.
from core.cases import case_trail  # noqa: E402,F401
from core.cases import intake_return  # noqa: E402,F401
