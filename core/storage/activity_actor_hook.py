"""Who to name on a case activity written from storage.

``core.storage`` cannot import ``core.cases``, and an activity's actor is
``core.cases``' to name, so ``core.cases`` installs the resolver. With none
installed the activity is still written, attributed to ``agent``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

_resolver: Optional[Callable[[Any], dict]] = None


def install(resolver: Callable[[Any], dict]) -> None:
    """``resolver(session)`` returns the actor fields for one new activity."""
    global _resolver
    _resolver = resolver


def fields(session: Any) -> dict:
    """Actor fields for an activity appended in ``session``."""
    if _resolver is None:
        return {"actor_text": "agent"}
    return dict(_resolver(session))
