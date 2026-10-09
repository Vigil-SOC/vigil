"""Each state-changing route demands the role permission that matches its effect.

``Auth.REQUIRED`` only proves a login: the seeded Viewer is read-only, and the
Analyst may not change settings or release response actions. A request from a
user who holds nothing must be refused with the permission named, before the
handler (and so before any database or integration) is reached.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

from core.storage.models import User  # noqa: E402
from services.api import main as backend_main  # noqa: E402
from services.api.middleware import auth as auth_module  # noqa: E402

pytestmark = pytest.mark.unit

RUN_ID = "9c1c2d3e-0000-4000-8000-000000000592"

# (method, path, body, permission the route must ask for)
GATED_ROUTES = [
    ("POST", "/api/config/postgresql", {"connection_string": "x"}, "settings.write"),
    ("POST", "/api/config/github", {"token": "x"}, "settings.write"),
    ("POST", "/api/config/claude", {"api_key": "x"}, "settings.write"),
    ("POST", "/api/config/secrets/reinit", {}, "settings.write"),
    ("POST", "/api/config/secrets/migrate-to-encrypted", None, "settings.write"),
    ("POST", "/api/config/force-manual-approval", {"enabled": True}, "settings.write"),
    ("POST", "/api/config/integrations", {"integrations": {}}, "integrations.write"),
    ("POST", "/api/config/darktrace", {}, "integrations.write"),
    ("POST", "/api/orchestrator/enable", None, "settings.write"),
    ("POST", "/api/orchestrator/disable", None, "settings.write"),
    ("POST", "/api/orchestrator/kill", None, "settings.write"),
    ("POST", "/api/orchestrator/investigations/purge", None, "settings.write"),
    ("DELETE", "/api/findings/all", None, "findings.delete"),
    ("PUT", "/api/analytics/budget", {}, "settings.write"),
    ("POST", "/api/v1/cases", {"title": "t"}, "cases.write"),
    ("PATCH", "/api/v1/cases/c-1", {"status": "closed"}, "cases.write"),
    ("POST", "/api/v1/cases/c-1/close", {}, "cases.write"),
    ("POST", "/api/v1/cases/c-1/merge", {"source_case_id": "c-2"}, "cases.write"),
    ("POST", "/api/cases", {"title": "t"}, "cases.write"),
    ("POST", "/api/cases/c-1/comments", {"content": "x"}, "cases.write"),
    ("DELETE", "/api/cases/c-1", None, "cases.delete"),
    ("PATCH", "/api/v1/findings/f-1", {"severity": "low"}, "findings.write"),
    ("POST", "/api/v1/approvals/a-1/approve", {}, "ai_decisions.approve"),
    ("POST", "/api/v1/approvals/a-1/reject", {"reason": "no"}, "ai_decisions.approve"),
    ("POST", "/api/workflows/runs/r-1/resume", {}, "ai_decisions.approve"),
    (
        "POST",
        "/api/workflows/runs/r-1/cancel",
        {"reason": "no"},
        "ai_decisions.approve",
    ),
    (
        "POST",
        "/api/v1/agent-runs",
        {"run_kind": "hunt", "playbook": "p", "config": "c"},
        "ai_chat.use",
    ),
    (
        "POST",
        f"/api/v1/agent-runs/{RUN_ID}/directives",
        {"kind": "note", "text": "x"},
        "ai_chat.use",
    ),
    ("POST", "/api/workflows/w-1/execute", {}, "ai_chat.use"),
    ("POST", "/api/claude/chat/stream", {"messages": []}, "ai_chat.use"),
]


@pytest.mark.parametrize("method,path,body,permission", GATED_ROUTES)
def test_a_route_refuses_a_user_who_lacks_its_permission(
    method, path, body, permission, monkeypatch
):
    viewer = User(
        user_id="viewer",
        username="vera_viewer",
        email="v@test.local",
        password_hash="",
        role_id="role-viewer",
        is_active=True,
        mfa_enabled=False,
    )

    # Holds everything except the one permission under test.
    def _check(user_id, perm, session=None):
        return perm != permission

    monkeypatch.setattr("core.auth.auth_service.AuthService.check_permission", _check)
    app = backend_main.app
    app.dependency_overrides[auth_module.get_current_active_user] = lambda: viewer
    app.dependency_overrides[auth_module.get_current_user] = lambda: viewer
    try:
        response = TestClient(app).request(method, path, json=body)
    finally:
        app.dependency_overrides.pop(auth_module.get_current_active_user, None)
        app.dependency_overrides.pop(auth_module.get_current_user, None)

    assert response.status_code == 403, (method, path, response.text[:200])


def test_every_config_write_asks_for_a_permission():
    """A new POST on /api/config cannot be added without a gate."""
    from services.api.routers.config import router

    def gated(route) -> bool:
        return any(
            getattr(dep.call, "__qualname__", "").startswith("require_permission.")
            for dep in route.dependant.dependencies
        )

    # The integration test route checks inside its handler.
    handler_checked = {"/integrations/{integration_id}/test"}
    ungated = [
        route.path
        for route in router.routes
        if route.methods - {"GET", "HEAD", "OPTIONS"}
        and route.path not in handler_checked
        and not gated(route)
    ]
    assert ungated == []


# --- Every write route makes a permission decision --------------------------

AUTH_ONLY_ROUTES = Path(__file__).with_name("auth_only_write_routes.txt")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Calls a handler makes when it checks the role itself rather than via Depends.
_INLINE_GUARDS = (
    "require_settings_admin",
    "require_integrations_admin",
    "_require_permission",
    "check_permission",
    "username_has_permission",
    "require_permission",
)


def _dependencies(dependant):
    for dep in dependant.dependencies:
        yield dep
        yield from _dependencies(dep)


def _write_routes_without_a_guard() -> set[str]:
    """``METHOD path`` for each state-changing /api route with no visible guard."""
    app = backend_main.app
    found: set[str] = set()

    def visit(obj) -> None:
        # Lazy included-router entries on FastAPI >= 0.137; see test_route_auth_coverage.
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        path = getattr(obj, "path", None)
        writes = set(getattr(obj, "methods", None) or ()) - _SAFE_METHODS
        if not isinstance(path, str) or not path.startswith("/api/") or not writes:
            return
        if any(
            getattr(dep.call, "__qualname__", "").startswith("require_permission.")
            for dep in _dependencies(obj.dependant)
        ):
            return
        try:
            source = inspect.getsource(obj.endpoint)
        except (OSError, TypeError):
            source = ""
        if any(guard in source for guard in _INLINE_GUARDS):
            return
        for method in writes:
            found.add(f"{method} {getattr(obj, 'path_format', None) or path}")

    for route in app.routes:
        visit(route)
    return found


def test_a_write_route_without_a_permission_guard_is_on_the_reviewed_list():
    """A new write route must ask for a permission or be listed as auth-only.

    ``test_route_auth_coverage`` proves a login; this proves someone decided what
    the login's role may do. The list is the set of routes that had no visible
    guard when this test was added (some check inside a helper the handler calls,
    some are login-only on purpose). Adding a line is a decision to review.
    """
    listed = {
        line.strip()
        for line in AUTH_ONLY_ROUTES.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    unguarded = _write_routes_without_a_guard()

    assert not unguarded - listed, (
        "Write routes with no permission guard and not in "
        f"{AUTH_ONLY_ROUTES.name}; add a permission_gate(...) or list them:\n  - "
        + "\n  - ".join(sorted(unguarded - listed))
    )
    assert not listed - unguarded, (
        f"{AUTH_ONLY_ROUTES.name} lists routes that are now guarded or gone; "
        "remove them:\n  - " + "\n  - ".join(sorted(listed - unguarded))
    )
