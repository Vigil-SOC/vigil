"""Whether Medic is meant to be running, as the backend sees it (C5, C8).

``VIGIL_MEDIC_ENABLED`` is the master switch on every install shape, and the
backend reads the same setting Medic does, so a Medic that is off never shows as
down. Medic's rule (``services/medic/app/config.py``) is copied, not imported:
Medic imports nothing from ``core`` and the backend nothing from Medic, and
``tests/unit/platform/test_medic_status.py`` holds the two to the same answers.

This module never touches the network. V2 adds the poller (through Medic's
gateway) and the ``running`` and ``down`` states.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from core.config import Settings, get_settings

# Exactly Medic's "on" spellings. Anything else, unrecognised values included,
# is off: the same fail-safe reading Medic makes.
_TRUE = frozenset({"true", "1", "yes", "on"})


class MedicStatus(str, Enum):
    OFF = "off"  # the flag is false: Medic exits at start, nothing is polled
    UNKNOWN = "unknown"  # the flag is true; nothing has seen Medic yet (V2)


def medic_enabled(settings: Optional[Settings] = None) -> bool:
    value = (settings or get_settings()).vigil_medic_enabled
    return value.strip().lower() in _TRUE


def medic_status(settings: Optional[Settings] = None) -> MedicStatus:
    if not medic_enabled(settings):
        return MedicStatus.OFF
    return MedicStatus.UNKNOWN
