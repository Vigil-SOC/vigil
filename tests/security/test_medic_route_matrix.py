"""Role x route matrix for the Medic proxy routes (C6/V3; H3 fills it in).

Medic can't tell Vigil roles apart: it trusts whoever holds ``X-Medic-Key``, and
only the backend does. So the backend's ``/api/medic/*`` routes are the only
place a Viewer is kept away from Medic's data and an Analyst from its one write
(K1 T-27). Each X2 operation names the permission its route must check
(``x-medic-permission`` in ``services/medic/contracts/medic-api.openapi.yaml``).

This file pins three things before any route exists:

* one row per X2 operation, with its permission. An operation added to the
  contract without a row fails here;
* every ``/api/medic`` route in the app has a row, so H3 can't add a route
  that the matrix doesn't cover;
* which seeded role holds which Medic permission (``MEDIC_GRANTS``; the seed
  itself is pinned against a real database in
  ``tests/unit/storage/test_medic_permissions_db.py``).

The role x route cases skip until H3 fills in a row's backend route.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

from core.auth.permissions import (  # noqa: E402
    MEDIC_ADMIN_PERMISSION,
    MEDIC_READ_PERMISSION,
)
from core.storage.models import User  # noqa: E402
from services.api import main as backend_main  # noqa: E402
from services.api.middleware import auth as auth_module  # noqa: E402

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
X2_SPEC = REPO / "services" / "medic" / "contracts" / "medic-api.openapi.yaml"
H3_TODO = "H3 (outputs/A2-issues/H3.md) builds this route and fills in its row"

# Which seeded role holds which Medic permission (V3, PROVISIONAL V3-1).
# A role not listed holds neither.
MEDIC_GRANTS: dict[str, frozenset[str]] = {
    "role-admin": frozenset({MEDIC_READ_PERMISSION, MEDIC_ADMIN_PERMISSION}),
    "role-manager": frozenset({MEDIC_READ_PERMISSION}),
    "role-senior-analyst": frozenset(),
    "role-analyst": frozenset(),
    "role-viewer": frozenset(),
}

# One row per X2 operation:
# (operationId, X2 method, X2 path, permission, backend method, backend path, body).
# H3 fills in the backend method and path (and a body for a write) when it builds
# the route; until then the role cases for that row skip.
MEDIC_ROUTES = [
    ("getStatus", "GET", "/v1/status", MEDIC_READ_PERMISSION, None, None, None),
    ("listIncidents", "GET", "/v1/incidents", MEDIC_READ_PERMISSION, None, None, None),
    (
        "getIncident",
        "GET",
        "/v1/incidents/{incident_id}",
        MEDIC_READ_PERMISSION,
        None,
        None,
        None,
    ),
    (
        "postFeedback",
        "POST",
        "/v1/incidents/{incident_id}/feedback",
        MEDIC_ADMIN_PERMISSION,
        None,
        None,
        None,
    ),
    ("listDecisions", "GET", "/v1/decisions", MEDIC_READ_PERMISSION, None, None, None),
    (
        "getDecision",
        "GET",
        "/v1/decisions/{seq}",
        MEDIC_READ_PERMISSION,
        None,
        None,
        None,
    ),
    ("listSensors", "GET", "/v1/sensors", MEDIC_READ_PERMISSION, None, None, None),
    ("getPack", "GET", "/v1/pack", MEDIC_READ_PERMISSION, None, None, None),
    (
        "getEvidenceExport",
        "GET",
        "/v1/export",
        MEDIC_ADMIN_PERMISSION,
        None,
        None,
        None,
    ),
]

_X2_PERMISSION = {"read": MEDIC_READ_PERMISSION, "admin": MEDIC_ADMIN_PERMISSION}
_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


def _x2_operations() -> set[tuple[str, str, str, str]]:
    spec = yaml.safe_load(X2_SPEC.read_text(encoding="utf-8"))
    return {
        (
            op["operationId"],
            method.upper(),
            path,
            _X2_PERMISSION[op["x-medic-permission"]],
        )
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in _HTTP_METHODS
    }


def test_the_matrix_has_one_row_per_x2_operation_with_its_permission():
    rows = {row[:4] for row in MEDIC_ROUTES}
    assert len(rows) == len(MEDIC_ROUTES), "duplicate rows"
    assert rows == _x2_operations()


def test_every_x2_write_needs_medic_admin():
    # UX-5: every write is medic.admin. A read may be either (export is admin).
    for op_id, method, _, permission, *_ in MEDIC_ROUTES:
        if method != "GET":
            assert permission == MEDIC_ADMIN_PERMISSION, op_id


def _medic_routes_in_app() -> set[str]:
    found: set[str] = set()

    def visit(obj) -> None:
        # Lazy included-router entries on FastAPI >= 0.137; see test_route_auth_coverage.
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        path = getattr(obj, "path_format", None) or getattr(obj, "path", None)
        if not isinstance(path, str) or not path.startswith("/api/medic"):
            return
        for method in set(getattr(obj, "methods", None) or ()) - {"HEAD", "OPTIONS"}:
            found.add(f"{method} {path}")

    for route in backend_main.app.routes:
        visit(route)
    return found


def test_every_medic_route_in_the_app_has_a_matrix_row():
    covered = {f"{method} {path}" for *_, method, path, _body in MEDIC_ROUTES if path}
    assert (
        _medic_routes_in_app() <= covered
    ), "Every /api/medic route needs a row in MEDIC_ROUTES (H3)"


def test_the_grants_cover_every_seeded_role_and_only_medic_permissions():
    assert set(MEDIC_GRANTS) == {
        "role-viewer",
        "role-analyst",
        "role-senior-analyst",
        "role-manager",
        "role-admin",
    }
    for grants in MEDIC_GRANTS.values():
        assert grants <= {MEDIC_READ_PERMISSION, MEDIC_ADMIN_PERMISSION}
        # medic.admin without medic.read would show buttons on a screen you
        # can't open.
        if MEDIC_ADMIN_PERMISSION in grants:
            assert MEDIC_READ_PERMISSION in grants


_CASES = [
    pytest.param(role, row, id=f"{role}-{row[0]}")
    for role in sorted(MEDIC_GRANTS)
    for row in MEDIC_ROUTES
]


@pytest.mark.parametrize("role,row", _CASES)
def test_role_x_route(role, row, monkeypatch):
    op_id, _, _, permission, method, path, body = row
    if path is None:
        pytest.skip(f"{op_id}: {H3_TODO}")

    user = User(
        user_id=f"u-{role}",
        username=f"u_{role}",
        email=f"{role}@test.local",
        password_hash="",
        role_id=role,
        is_active=True,
        mfa_enabled=False,
    )

    def _check(user_id, perm, session=None):
        return perm in MEDIC_GRANTS[role]

    monkeypatch.setattr("core.auth.auth_service.AuthService.check_permission", _check)
    app = backend_main.app
    app.dependency_overrides[auth_module.get_current_active_user] = lambda: user
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    try:
        response = TestClient(app).request(method, path, json=body)
    finally:
        app.dependency_overrides.pop(auth_module.get_current_active_user, None)
        app.dependency_overrides.pop(auth_module.get_current_user, None)

    if permission in MEDIC_GRANTS[role]:
        assert response.status_code != 403, (role, op_id, response.text[:200])
    else:
        assert response.status_code == 403, (role, op_id, response.text[:200])
        assert permission in response.text, (role, op_id, response.text[:200])
