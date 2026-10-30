"""The trail entry a case-row write appends (#3403).

``core.storage`` may not import ``core.cases`` (.importlinter tiers), and an
entry's actor is ``core.cases``' to name, so ``core.cases`` installs its writer
here when it is imported. With the trail in use and no writer, a case write
fails rather than land unrecorded.
"""

from __future__ import annotations

from typing import Any, Optional

from core.audit import trail


class TrailWriteError(Exception):
    """A trail append failed. A joined caller must not commit past it."""


# What a case write raises to its caller instead of answering False or None.
RAISED = (TrailWriteError, trail.Paused)

_writer: Any = None


def install(writer: Any) -> None:
    """``writer`` has ``append_case`` and ``append_update`` (core.cases.case_trail)."""
    global _writer
    _writer = writer


def writer(session) -> Optional[Any]:
    """The installed writer; None only when the trail is not in use either."""
    if _writer is not None:
        return _writer
    try:
        in_use = trail.in_use(session)
    except Exception as exc:
        raise TrailWriteError(str(exc)) from exc
    if in_use:
        raise TrailWriteError("the case trail writer is not installed")
    return None
