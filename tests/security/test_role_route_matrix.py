"""Role x route matrix: each seeded role meets each declared gate on real auth.

The routes and their permissions are read off the mounted app, and the role
grants out of ``06_auth_tables.sql``, so neither list can drift from the code.
Requests carry a real access token for a real user row and go through the real
``get_current_user`` and ``AuthService.check_permission``; only the database
and the Redis revocation lookup are swapped. A role that lacks a route's
permission must get 403 naming it, before any handler runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import current_user as current_user_module
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from core.storage.models import Principal, Role, User
from core.storage.models.base import Base
from services.api import main as backend_main
from tests.security.test_route_permissions import _dependencies

pytestmark = pytest.mark.unit

ROLES_SQL = (
    Path(__file__).parents[2] / "infra" / "database" / "init" / "06_auth_tables.sql"
)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _seeded_roles() -> dict[str, dict[str, bool]]:
    """``{role_id: permissions}`` as the init SQL seeds them, plus revision grants.

    ``06`` is the baseline seed. Later revisions add keys (``0002`` adds the
    audit permissions). Existing keys win, matching ``grants || permissions``.
    """
    rows = re.findall(
        r"\('(role-[\w-]+)', '[^']+', '[^']*', '(\{.*?\})', true\)",
        ROLES_SQL.read_text(),
        re.DOTALL,
    )
    roles = {role_id: json.loads(grants) for role_id, grants in rows}
    revisions = Path(__file__).parents[2] / "core" / "upgrade" / "versions"
    for path in sorted(revisions.glob("*.sql")):
        for role_id, grants in re.findall(
            r"\('(role-[\w-]+)',\s*'(\{.*?\})'::jsonb\)",
            path.read_text(),
            re.DOTALL,
        ):
            if role_id in roles:
                roles[role_id] = {**json.loads(grants), **roles[role_id]}
    return roles


def _gated_routes() -> list[tuple[str, str, tuple[tuple[str, bool], ...]]]:
    """``(method, path, ((permission, unsafe_only), ...))`` per gated /api route."""
    found = []

    def visit(obj) -> None:
        # Lazy included-router entries on FastAPI >= 0.137.
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        path = getattr(obj, "path", None)
        methods = getattr(obj, "methods", None)
        if not isinstance(path, str) or not path.startswith("/api/") or not methods:
            return
        gates = tuple(
            (dep.call.permission, dep.call.unsafe_only)
            for dep in _dependencies(obj.dependant)
            if hasattr(dep.call, "permission")
        )
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            if gates:
                found.append((method, path, gates))

    for route in backend_main.app.routes:
        visit(route)
    return found


ROLES = _seeded_roles()
ROUTES = _gated_routes()


def _applicable(method: str, gates) -> list[str]:
    """Permissions the route demands for ``method`` (unsafe_only gates skip reads)."""
    return [
        p for p, unsafe_only in gates if not (unsafe_only and method in _SAFE_METHODS)
    ]


def _denied_cases():
    cases = []
    for role_id, grants in ROLES.items():
        for method, path, gates in ROUTES:
            missing = [p for p in _applicable(method, gates) if not grants.get(p)]
            if missing:
                cases.append(
                    pytest.param(
                        role_id, method, path, missing, id=f"{role_id}:{method} {path}"
                    )
                )
    return cases


@pytest.fixture(scope="module")
def maker():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[Principal.__table__, Role.__table__, User.__table__]
    )
    maker = sessionmaker(bind=engine)
    with maker() as s:
        for role_id, grants in ROLES.items():
            s.add(
                Role(role_id=role_id, name=role_id, description="", permissions=grants)
            )
            # A person, so the audit.forward kind check reaches the role grant.
            s.add(Principal(principal_id=f"u-{role_id}", kind="person", label=role_id))
            s.add(
                User(
                    user_id=f"u-{role_id}",
                    principal_id=f"u-{role_id}",
                    username=role_id,
                    email=f"{role_id}@example.com",
                    password_hash="x",
                    full_name=role_id,
                    role_id=role_id,
                    is_active=True,
                )
            )
        s.commit()
    return maker


@pytest.fixture
def client(maker, monkeypatch):
    def _session():
        with maker() as s:
            yield s

    async def _not_revoked(payload):
        return False

    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr(current_user_module, "is_token_revoked", _not_revoked)
    # check_permission opens its own session through the unit of work.
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", maker)
    backend_main.app.dependency_overrides[request_unit_of_work] = _session
    try:
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(request_unit_of_work, None)


def _bearer(maker, role_id: str) -> dict[str, str]:
    with maker() as s:
        user = s.get(User, f"u-{role_id}")
        # Poll refuses a token with no sign-in before it consults the role.
        token = AuthService.generate_jwt_token(user, "access", sid="si-matrix")
    return {"Authorization": f"Bearer {token}"}


def _filled(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_the_matrix_is_not_vacuous():
    assert len(ROLES) == 5
    assert len(ROUTES) > 50


def test_every_gate_names_a_permission_some_seeded_role_holds():
    """A typo like ``finding.delete`` would lock out everyone but DEV_MODE."""
    held = {p for grants in ROLES.values() for p, on in grants.items() if on}
    unknown = {p for _, _, gates in ROUTES for p, _ in gates} - held
    assert not unknown


@pytest.mark.parametrize("role_id,method,path,missing", _denied_cases())
def test_a_role_without_the_permission_is_refused(
    client, maker, role_id, method, path, missing
):
    response = client.request(method, _filled(path), headers=_bearer(maker, role_id))

    assert response.status_code == 403, (role_id, method, path, response.text[:200])
    assert response.json()["detail"] in {
        f"Permission denied: {p} required" for p in missing
    }


def test_check_permission_matches_the_seeded_grant(client, maker):
    """Holders are asserted through ``check_permission``, not by firing handlers."""
    for role_id, grants in ROLES.items():
        for method, _, gates in ROUTES:
            for perm in _applicable(method, gates):
                assert AuthService.check_permission(f"u-{role_id}", perm) == grants.get(
                    perm, False
                ), (role_id, perm)


def test_a_gated_route_refuses_an_unauthenticated_caller(client):
    method, path, _ = ROUTES[0]
    assert client.request(method, _filled(path)).status_code == 401
