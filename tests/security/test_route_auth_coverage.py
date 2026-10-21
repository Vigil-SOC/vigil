"""Route inventory: every /api/* route must require auth or be on the
explicit public allowlist.

Locks in the deny-by-default contract introduced after the 2026-05
security disclosure. If you add a new router or route without auth,
this test fails — and the fix is either to add ``dependencies=AUTH_DEPENDENCY``
to the include_router call (or ``Depends(get_current_active_user)`` to
the handler) or, if the route is intentionally public, to add it to
``PUBLIC_API_PATHS`` in ``services/api/main.py``.

Adding a route to ``PUBLIC_API_PATHS`` is a security decision, not a way
to make this test quiet. A feature-flagged inbound webhook receiver that
shows up here because it was mounted unconditionally must be fixed by
restoring the flag, not by allowlisting the path.

Traversal note (issue #532): FastAPI >= 0.137 no longer flattens child
routes into ``app.routes``. It stores lazy ``_IncludedRouter`` entries,
which have no ``.path`` attribute, so the ``for route in app.routes``
loop this test used to run examined 1 route out of 357 — ``/api/health``,
already on the allowlist — and passed unconditionally. Because
``requirements.txt`` had no upper bound on ``fastapi``, a transitive
upgrade disarmed the gate with no visible signal.
``_collect_api_routes`` walks both table shapes, and
``test_route_inventory_reaches_every_documented_path`` fails if the walk
ever goes stale against a future version.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

# Importing services.api.main pulls in auth_service, which refuses to load
# without a JWT secret once DEV_MODE is false — the default anywhere
# without a .env, CI included. Set it here so this file stands alone; it
# previously only worked because test_unauth_endpoints.py happens to call
# os.environ.setdefault at collection time.
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


def _collect_api_routes(app, auth_deps) -> list[tuple[str, str, bool]]:
    """Return ``(path, method_label, has_auth)`` for every effective /api/ route.

    Handles both route-table shapes: modern FastAPI, where ``app.routes``
    holds lazy ``_IncludedRouter`` entries whose ``effective_candidates()``
    yields per-route contexts (and, where a router includes another router,
    further ``_IncludedRouter`` entries — hence the recursion); and older
    FastAPI, which flattens child routes into ``app.routes`` directly.

    Either way the object carrying ``.path`` also carries a ``.dependant``
    with mount-site ``dependencies=`` already folded in, so router-level and
    route-level auth are both visible from one chain walk.
    """
    collected: list[tuple[str, str, bool]] = []

    def visit(obj) -> None:
        # Compared by name rather than imported: the class is private and
        # absent on older FastAPI, where an import would break collection.
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():  # a method, not a list
                visit(candidate)
            return

        path = getattr(obj, "path", None)
        if not isinstance(path, str) or not path.startswith("/api/"):
            return

        methods = getattr(obj, "methods", None)
        collected.append(
            (
                # path_format renders convertors the way OpenAPI documents
                # them: /api/f/{name:path} -> /api/f/{name}.
                getattr(obj, "path_format", None) or path,
                "/".join(sorted(methods)) if methods else type(obj).__name__,
                _walk_dependants(getattr(obj, "dependant", None), auth_deps),
            )
        )

    for route in app.routes:
        visit(route)

    return collected


def _auth_deps():
    from services.api.middleware.auth import get_current_active_user, get_current_user

    return {get_current_active_user, get_current_user}


def _collect_routes(app) -> list:
    """Every route object, including ones outside ``/api/``.

    ``/metrics`` is the only non-``/api/`` path this file inspects. Walking
    the rest (``/docs``, ``/mcp``, ``/internal``) is a separate decision.
    """
    found = []

    def visit(obj) -> None:
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        if isinstance(getattr(obj, "path", None), str):
            found.append(obj)

    for route in app.routes:
        visit(route)
    return found


def _dependency_calls(dependant):
    """Every ``call`` on ``dependant`` and the dependencies mounted above it."""
    if dependant is None:
        return
    if dependant.call is not None:
        yield dependant.call
    for dep in dependant.dependencies:
        yield from _dependency_calls(dep)


def test_metrics_route_requires_the_scrape_bearer():
    """``GET /metrics`` is not a public path and not a user session.

    The scrape bearer is its own dependency, so this inventory sees it
    without treating every non-``/api/`` route as in scope. Matched by
    module and name: ``test_monitoring`` reloads ``core.platform.monitoring``
    after the app has bound the function it imported, so identity fails
    once that test has already run.
    """
    from services.api.main import PUBLIC_API_PATHS, app

    assert "/metrics" not in PUBLIC_API_PATHS
    routes = [r for r in _collect_routes(app) if r.path == "/metrics"]
    assert len(routes) == 1
    assert any(
        getattr(call, "__module__", None) == "core.platform.monitoring"
        and getattr(call, "__name__", None) == "require_metrics_token"
        for call in _dependency_calls(routes[0].dependant)
    )


def test_every_api_route_requires_auth_or_is_explicitly_public():
    # Import lazily so a broken main.py shows as a test failure rather
    # than a collection error.
    from services.api.main import PUBLIC_API_PATHS, app

    missing = {
        f"{method} {path}"
        for path, method, has_auth in _collect_api_routes(app, _auth_deps())
        if not has_auth and not _is_public(path, PUBLIC_API_PATHS)
    }

    assert (
        not missing
    ), "Routes without auth (and not on PUBLIC_API_PATHS):\n  - " + "\n  - ".join(
        sorted(missing)
    )


def test_route_inventory_reaches_every_documented_path():
    """Guard the guard: fail if the traversal stops reaching the route table.

    The failure this exists to catch is not "a route lost its auth" — it is
    "the check silently stopped checking". ``app.openapi()`` is public,
    stable API, so it is a self-calibrating oracle for what the walk above
    must reach: no threshold to rot as the API grows, and the next breaking
    FastAPI change lands as a red test instead of a silent green one.
    """
    from services.api.main import app

    documented = {p for p in app.openapi()["paths"] if p.startswith("/api/")}
    # Without this, an OpenAPI generation failure would make the comparison
    # below pass vacuously on two empty sets.
    assert documented, "app.openapi() reported no /api/ paths at all"

    reached = {path for path, _method, _auth in _collect_api_routes(app, _auth_deps())}

    # Subset, not equality: the walk also sees routes kept out of the schema
    # with include_in_schema=False.
    unreached = sorted(documented - reached)
    assert not unreached, (
        f"the route walk missed {len(unreached)} of {len(documented)} documented "
        f"/api/ paths, so it is no longer inspecting the real route table "
        f"(see issue #532). Fix the traversal — do not relax this assertion:\n  - "
        + "\n  - ".join(unreached)
    )
