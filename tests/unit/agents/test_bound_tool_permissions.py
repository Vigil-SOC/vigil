"""A credential does what its user may do now, not what they could do when it was minted.

Write tools on the MCP surface and the shared backend door both check the
bound principal's live role. A hunt binds nobody, and is not refused for it.
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from core.agents.tool_registry import execute_backend_tool
from core.cases.case_workflow_service import CaseWorkflowService
from core.integrations.mcp.surface import acting_as
from core.storage.models import Role, User
from core.storage.models.base import Base
from tools.mcp import vigil


# `roles.permissions` is JSONB. SQLite cannot render that type; this compile
# is registered for SQLite only.
@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


# Every mutating Vigil tool, and the permission the HTTP route of the same
# write requires. A new write tool has to be added here or the demotion test
# does not call it.
WRITE_TOOLS = (
    ("dismiss_finding", "findings.write"),
    ("create_case", "cases.write"),
    ("update_case", "cases.write"),
    ("add_finding_to_case", "cases.write"),
    ("remove_finding_from_case", "cases.write"),
    ("add_case_activity", "cases.write"),
    ("add_case_timeline_entry", "cases.write"),
    ("add_case_mitre_techniques", "cases.write"),
    ("add_resolution_step", "cases.write"),
    ("bulk_add_findings_to_case", "cases.write"),
    ("create_case_from_killchain", "cases.write"),
    ("add_case_comment", "cases.write"),
    ("add_case_evidence", "cases.write"),
    ("add_case_ioc", "cases.write"),
    ("bulk_add_iocs", "cases.write"),
    ("add_case_task", "cases.write"),
    ("update_case_task", "cases.write"),
    ("link_related_cases", "cases.write"),
    ("escalate_case", "cases.write"),
    ("close_case", "cases.write"),
)

# The same functions reached from chat and hunts, without the MCP wrapper.
BACKEND_WRITES = (
    ("dismiss_finding", "findings.write", {"finding_id": "f-1", "reason": "noise"}),
    ("create_case", "cases.write", {"title": "t"}),
    (
        "update_case",
        "cases.write",
        {"case_id": "c-1"},
    ),
    (
        "add_finding_to_case",
        "cases.write",
        {"case_id": "c-1", "finding_id": "f-1"},
    ),
    (
        "add_resolution_step",
        "cases.write",
        {"case_id": "c-1", "description": "d", "action_taken": "a"},
    ),
)


def _invoke(name: str) -> dict:
    fn = getattr(vigil, name)
    kwargs = {}
    for pname, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        if pname.endswith("ids") or pname in {"iocs", "killchain_stages"}:
            kwargs[pname] = []
        else:
            kwargs[pname] = "x"
    return json.loads(fn(**kwargs))


class _Cases:
    def create_case(self, **kwargs):
        return {"case_id": "c-1", "title": kwargs.get("title"), "status": "new"}

    def get_cases(self):
        return []

    def get_case(self, case_id):
        return {"case_id": case_id, "title": "t", "status": "new", "notes": []}


@pytest.fixture
def directory(monkeypatch):
    """Two roles and one user, in the store the permission check reads."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Role.__table__, User.__table__])
    session = sessionmaker(bind=engine)()
    session.add(
        Role(
            role_id="r-senior",
            name="senior",
            description="",
            permissions={
                "cases.write": True,
                "findings.write": True,
                "integrations.write": True,
            },
        )
    )
    session.add(
        Role(
            role_id="r-viewer",
            name="viewer",
            description="",
            permissions={"findings.read": True, "cases.read": True},
        )
    )
    user = User(
        user_id="u-1",
        principal_id="u-1",
        username="nestor",
        email="nestor@example.com",
        password_hash="x",
        full_name="Nestor",
        role_id="r-senior",
        is_active=True,
    )
    session.add(user)
    session.commit()

    @contextmanager
    def _this_store(_session=None):
        yield session

    monkeypatch.setattr("core.auth.permissions.unit_of_work", _this_store)
    monkeypatch.setattr("core.auth.auth_service.unit_of_work", _this_store)
    monkeypatch.setattr("core.agents.tool_registry._data", lambda: _Cases())
    yield session, user
    session.close()


def test_demotion_is_read_before_the_next_write(directory, monkeypatch):
    session, user = directory
    closed = []

    def _close(self, *args, **kwargs):
        closed.append(kwargs)
        return None

    monkeypatch.setattr(CaseWorkflowService, "close_case", _close)

    with acting_as("nestor"):
        allowed = _invoke("create_case")
        assert allowed["success"] is True
        assert allowed["case_id"] == "c-1"

        user.role_id = "r-viewer"
        session.commit()

        for name, permission in WRITE_TOOLS:
            refused = _invoke(name)
            assert refused == {
                "error": f"nestor may not do this: {permission} required"
            }, name

    assert closed == []


def test_a_hunt_has_no_role_to_lack(directory):
    created = _invoke("create_case")

    assert created["success"] is True
    assert created["case_id"] == "c-1"


def test_a_demoted_principal_can_still_read(directory):
    session, user = directory
    user.role_id = "r-viewer"
    session.commit()

    with acting_as("nestor"):
        listed = _invoke("list_cases")

    assert listed == []


@pytest.mark.parametrize("tool,permission,args", BACKEND_WRITES)
async def test_the_backend_door_refuses_a_demoted_principal(
    directory, tool, permission, args
):
    session, user = directory
    user.role_id = "r-viewer"
    session.commit()

    with acting_as("nestor"):
        result, handled = await execute_backend_tool(tool, args)

    assert handled is True
    assert result == {"error": f"nestor may not do this: {permission} required"}


async def test_the_backend_door_still_writes_for_a_hunt(directory):
    result, handled = await execute_backend_tool("create_case", {"title": "t"})

    assert handled is True
    assert result["success"] is True
    assert result["case_id"] == "c-1"
