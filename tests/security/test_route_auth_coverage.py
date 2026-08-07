"""Route inventory: every /api/* route must require auth or be on the
explicit public allowlist.

Locks in the deny-by-default contract introduced after the 2026-05
security disclosure. If you add a new router or route without auth,
this test fails — and the fix is either to add ``dependencies=AUTH_DEPENDENCY``
to the include_router call (or ``Depends(get_current_active_user)`` to
the handler) or, if the route is intentionally public, to add it to
``PUBLIC_API_PATHS`` in ``backend/main.py``.

Adding a route to ``PUBLIC_API_PATHS`` is a security decision, not a way
to make this test quiet. In particular, a feature-flagged inbound
webhook receiver that appears here because it was mounted
unconditionally must be fixed by restoring the flag, not by allowlisting
it.

Traversal note: FastAPI >= ~0.13x does not flatten child routes into
``app.routes``. It stores lazy ``_IncludedRouter`` entries which have no
``.path`` attribute, so the naive ``for route in app.routes`` loop this
test used to run silently examined 1 route out of ~359 and passed
unconditionally for months (issue #532). ``_collect_api_routes`` walks
both shapes, and ``test_route_inventory_is_not_vacuous`` fails if the
walk ever goes stale again.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

# Importing backend.main pulls in auth_service, which refuses to load
# without a JWT secret when DEV_MODE is false (the CI default). Set it
# here so this file stands alone — it previously only worked because
# test_unauth_endpoints.py happened to set it at collection time.
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit


def _is_public(path: str, public_patterns) -> bool:
    """Match ``path`` against the public allowlist (supports ``*`` wildcards)."""
    for pat in public_patterns:
        if pat == path:
            return True
        if fnmatch.fnmatch(path, pat):
            return True
    return False


def _walk_dependants(dependant, auth_deps) -> bool:
    """Recursively check a dependant chain for an auth dependency."""
    if dependant is None:
        return False
    for dep in dependant.dependencies:
        if dep.call in auth_deps:
            return True
        if _walk_dependants(dep, auth_deps):
            return True
    return False


def _mount_declares_auth(included_router, auth_deps) -> bool:
    """True if an ``_IncludedRouter``'s mount site passed an auth dependency.

    ``app.include_router(r, dependencies=AUTH_DEPENDENCY)`` is recorded on
    the include context rather than folded into each child route, so
    router-level auth has to be read from here and inherited downward.
    """
    ctx = getattr(included_router, "include_context", None)
    declared = getattr(ctx, "dependencies", None) or []
    # These are ``params.Depends`` instances (``.dependency``), not the
    # ``Dependant`` objects (``.call``) found on a route's dependant chain.
    return any(getattr(d, "dependency", None) in auth_deps for d in declared)


def _collect_api_routes(app, auth_deps) -> list[tuple[str, str, bool]]:
    """Return ``(path, method_label, has_auth)`` for every effective route.

    Handles both route-table shapes:

    * modern FastAPI — ``app.routes`` holds lazy ``_IncludedRouter`` entries
      whose ``effective_candidates()`` yields ``_EffectiveRouteContext``
      objects (and, where a router includes another router, further
      ``_IncludedRouter`` entries, so this recurses);
    * older FastAPI — child routes are flattened into ``app.routes`` already.
    """
    collected: list[tuple[str, str, bool]] = []

    def visit(obj, inherited_auth: bool) -> None:
        if type(obj).__name__ == "_IncludedRouter":
            mount_auth = inherited_auth or _mount_declares_auth(obj, auth_deps)
            # NB: a method, not a list.
            for candidate in obj.effective_candidates():
                visit(candidate, mount_auth)
            return

        path = getattr(obj, "path", None)
        if not isinstance(path, str) or not path:
            return

        # On an _EffectiveRouteContext, ``.path`` is the full prefixed path
        # while ``.original_route.path`` is the un-prefixed one; the dependant
        # lives on the original route.
        original = getattr(obj, "original_route", obj)
        has_auth = inherited_auth or _walk_dependants(
            getattr(original, "dependant", None), auth_deps
        )

        methods = getattr(obj, "methods", None)
        label = "/".join(sorted(methods)) if methods else type(obj).__name__
        collected.append((path, label, has_auth))

    for route in app.routes:
        visit(route, False)

    return [item for item in collected if item[0].startswith("/api/")]


def test_every_api_route_requires_auth_or_is_explicitly_public():
    # Import lazily so a broken main.py shows as a test failure rather
    # than a collection error.
    from backend.main import app, PUBLIC_API_PATHS
    from backend.middleware.auth import get_current_active_user, get_current_user

    auth_deps = {get_current_active_user, get_current_user}

    missing = [
        f"{method} {path}"
        for path, method, has_auth in _collect_api_routes(app, auth_deps)
        if not has_auth and not _is_public(path, PUBLIC_API_PATHS)
    ]

    assert (
        not missing
    ), "Routes without auth (and not on PUBLIC_API_PATHS):\n  - " + "\n  - ".join(
        sorted(set(missing))
    )


def test_route_inventory_is_not_vacuous():
    """Guard the guard: fail if the traversal stops finding routes.

    The failure this test exists to prevent is not "a route lost its auth"
    — it is "the check silently stopped checking". ``app.openapi()`` is a
    public, stable API, so it makes a self-calibrating oracle for how many
    paths the walk above should be reaching. No hardcoded threshold to rot
    as the API grows.
    """
    from backend.main import app
    from backend.middleware.auth import get_current_active_user, get_current_user

    auth_deps = {get_current_active_user, get_current_user}

    documented = [p for p in app.openapi()["paths"] if p.startswith("/api/")]
    # If OpenAPI generation itself breaks, the comparison below would pass
    # vacuously at 0 >= 0.
    assert documented, "app.openapi() reported no /api/ paths at all"

    found = {path for path, _method, _auth in _collect_api_routes(app, auth_deps)}

    # ``>=`` rather than ``==``: the walk also sees routes excluded from the
    # schema with include_in_schema=False, and Starlette path convertors
    # ({filename:path}) render differently in OpenAPI ({filename}).
    assert len(found) >= len(documented), (
        f"route traversal found {len(found)} /api/ paths but OpenAPI documents "
        f"{len(documented)} — the walk has gone stale against this FastAPI "
        f"version (see issue #532) and is no longer inspecting the real route "
        f"table. Do not silence this by lowering the bound."
    )
