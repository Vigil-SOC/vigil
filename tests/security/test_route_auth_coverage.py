"""Route inventory: every /api/* route must require auth or be on the
explicit public allowlist.

Locks in the deny-by-default contract introduced after the 2026-05
security disclosure. If you add a new router or route without auth,
this test fails — and the fix is either to add ``dependencies=AUTH_DEPENDENCY``
to the include_router call (or ``Depends(get_current_active_user)`` to
the handler) or, if the route is intentionally public, to add it to
``PUBLIC_API_PATHS`` in ``backend/main.py``.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


def _is_public(path: str, public_patterns) -> bool:
    """Match ``path`` against the public allowlist (supports ``*`` wildcards)."""
    for pat in public_patterns:
        if pat == path:
            return True
        if fnmatch.fnmatch(path, pat):
            return True
    return False


def _route_has_auth(route, auth_deps) -> bool:
    """Return True if ``route`` (router-included or directly mounted) has
    the active-user dependency anywhere in its dependant chain.

    FastAPI flattens router-level ``dependencies=[Depends(...)]`` into
    the route's ``dependant.dependencies`` list, so checking the leaf
    is sufficient for both router-level and route-level wiring.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    # Router-level deps land here as ``Dependant`` children with ``call``
    # set to our dependency function.
    for dep in dependant.dependencies:
        if dep.call in auth_deps:
            return True
    return False


def _walk_dependants(dependant, auth_deps):
    """Recursively check dependant chain for an auth dependency."""
    if dependant is None:
        return False
    for dep in dependant.dependencies:
        if dep.call in auth_deps:
            return True
        if _walk_dependants(dep, auth_deps):
            return True
    return False


def test_every_api_route_requires_auth_or_is_explicitly_public():
    # Import lazily so a broken main.py shows as a test failure rather
    # than a collection error.
    from backend.main import app, PUBLIC_API_PATHS
    from backend.middleware.auth import get_current_active_user, get_current_user

    auth_deps = {get_current_active_user, get_current_user}

    missing: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not isinstance(path, str) or not path.startswith("/api/"):
            continue
        methods = getattr(route, "methods", None)
        method_label = "/".join(sorted(methods)) if methods else type(route).__name__
        if _is_public(path, PUBLIC_API_PATHS):
            continue

        if _walk_dependants(getattr(route, "dependant", None), auth_deps):
            continue

        missing.append(f"{method_label} {path}")

    assert (
        not missing
    ), "Routes without auth (and not on PUBLIC_API_PATHS):\n  - " + "\n  - ".join(
        missing
    )
