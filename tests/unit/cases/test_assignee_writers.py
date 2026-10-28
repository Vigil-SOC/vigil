"""Every writer of a case or task assignee names one active person (#3155).

Template create, task create and update, the agent tools and the MCP task and
escalation tools resolve through ``core.cases.assignee``: a name that matches
no one, a deactivated account and a service account are refused with nothing
saved; blank clears. PATCH is in test_case_update_assignee.py.
"""

import contextlib
import json
from types import SimpleNamespace

import pytest

import tools.mcp.vigil as vigil
from core.agents import tool_registry
from core.cases import agent_closure, case_records_service
from core.cases.assignee import AssigneeRefused
from core.cases.case_templates_router import (
    CaseFromTemplate,
    create_case_from_template,
    workflow_service,
)
from core.cases.case_workflow_service import CaseWorkflowService
from services.api.routers import cases as cases_routes
from tests.unit.cases.people import people

pytestmark = pytest.mark.unit

REFUSED = ["no-such-person", "bob", "medic-bot"]


@pytest.fixture
def template(monkeypatch):
    made = []

    def create(**kwargs):
        made.append(kwargs)
        return None  # The route's 500 is not under test; what was passed is.

    monkeypatch.setattr(workflow_service, "create_case_from_template", create)
    return made


def _from_template(assignee):
    # Called directly, so FastAPI does not resolve the user dependency.
    return create_case_from_template(
        "t-1",
        CaseFromTemplate(title="x", assignee=assignee),
        people(),
        SimpleNamespace(user_id="alice"),
    )


async def test_a_template_case_stores_the_username(template):
    with contextlib.suppress(Exception):
        await _from_template("Alice@Example.com")
    assert template[0]["assignee"] == "alice"


@pytest.mark.parametrize("sent", REFUSED)
async def test_a_template_case_for_a_non_person_is_refused_before_create(
    template, sent
):
    with pytest.raises(AssigneeRefused):
        await _from_template(sent)
    assert template == []


async def test_a_template_case_with_a_blank_assignee_is_unassigned(template):
    with contextlib.suppress(Exception):
        await _from_template("  ")
    assert template[0]["assignee"] is None


@pytest.fixture
def tasks(monkeypatch):
    added = []
    task = SimpleNamespace(task_id=5, case_id="c-1", title="t", assignee="alice")
    monkeypatch.setattr(
        case_records_service,
        "add_task",
        lambda session, case_id, **kw: added.append(kw["assignee"]) or task,
    )
    monkeypatch.setattr(case_records_service, "update_task", lambda *a: task)
    monkeypatch.setattr(cases_routes.CaseTaskSchema, "dump", staticmethod(lambda t: {}))
    return added, task


async def test_task_create_stores_the_username(tasks):
    added, _ = tasks
    await cases_routes.add_task(
        "c-1", cases_routes.TaskAdd(title="t", assignee="ALICE"), people()
    )
    assert added == ["alice"]


@pytest.mark.parametrize("sent", REFUSED)
async def test_task_create_for_a_non_person_is_refused(tasks, sent):
    added, _ = tasks
    with pytest.raises(AssigneeRefused):
        await cases_routes.add_task(
            "c-1", cases_routes.TaskAdd(title="t", assignee=sent), people()
        )
    assert added == []


@pytest.mark.parametrize("sent", REFUSED)
async def test_task_update_for_a_non_person_is_refused(tasks, sent):
    _, task = tasks
    with pytest.raises(AssigneeRefused):
        cases_routes.update_task(
            "c-1", 5, cases_routes.TaskUpdate(assignee=sent), people()
        )
    assert task.assignee == "alice"


async def test_task_update_with_a_blank_assignee_clears_it(tasks):
    _, task = tasks
    cases_routes.update_task("c-1", 5, cases_routes.TaskUpdate(assignee=""), people())
    assert task.assignee is None


@pytest.fixture
def agent_tools(monkeypatch):
    """The agent tools against the throwaway users, with case storage stubbed."""
    opened, updates = [], []
    stored = {"case_id": "c-1", "assignee": "alice"}

    @contextlib.contextmanager
    def session():
        yield people()

    data = SimpleNamespace(
        get_case=lambda case_id: stored,
        update_case=lambda case_id, **kw: updates.append(kw) or True,
    )
    monkeypatch.setattr(agent_closure, "service_session", session)
    monkeypatch.setattr(tool_registry, "permission_refusal", lambda *a: None)
    monkeypatch.setattr(tool_registry, "_data", lambda: data)
    monkeypatch.setattr(
        tool_registry, "open_case", lambda *a, **kw: opened.append(kw) or stored
    )
    return opened, updates


@pytest.mark.parametrize("sent", REFUSED)
def test_agent_create_case_for_a_non_person_opens_no_case(agent_tools, sent):
    opened, updates = agent_tools
    result = tool_registry.create_case(title="x", assignee=sent)
    assert result == {"error": AssigneeRefused().message}
    assert opened == [] and updates == []


def test_agent_create_case_stores_the_username(agent_tools):
    _, updates = agent_tools
    tool_registry.create_case(title="x", assignee="alice@example.com")
    assert updates == [{"assignee": "alice"}]


@pytest.mark.parametrize("sent", REFUSED)
def test_agent_update_case_for_a_non_person_saves_nothing(agent_tools, sent):
    _, updates = agent_tools
    result = tool_registry.update_case(case_id="c-1", title="t", assignee=sent)
    assert "error" in result
    assert updates == []


def test_agent_update_case_with_a_blank_assignee_clears_it(agent_tools):
    _, updates = agent_tools
    tool_registry.update_case(case_id="c-1", assignee="")
    assert updates == [{"assignee": None}]


@pytest.fixture
def mcp(monkeypatch):
    task = SimpleNamespace(task_id=5, case_id="c-1", title="t", assignee="alice")
    added, escalated = [], []

    @contextlib.contextmanager
    def session():
        yield people()

    monkeypatch.setattr(vigil, "service_session", session)
    monkeypatch.setattr(
        case_records_service,
        "add_task",
        lambda session, case_id, **kw: added.append(kw["assignee"]) or task,
    )
    monkeypatch.setattr(case_records_service, "update_task", lambda *a: task)
    monkeypatch.setattr(vigil.CaseTaskSchema, "dump", staticmethod(lambda t: {}))
    monkeypatch.setattr(
        CaseWorkflowService,
        "escalate_case",
        lambda self, **kw: escalated.append(kw["escalated_to"]) or True,
    )
    monkeypatch.setattr(case_records_service, "list_escalations", lambda *a: [])
    monkeypatch.setattr(vigil, "add_case_activity", lambda *a: None)
    return task, added, escalated


@pytest.mark.parametrize("sent", REFUSED)
def test_mcp_task_writes_for_a_non_person_are_refused(mcp, sent):
    task, added, _ = mcp
    assert "error" in json.loads(vigil.add_case_task("c-1", "t", assignee=sent))
    assert "error" in json.loads(vigil.update_case_task(5, assignee=sent))
    assert added == [] and task.assignee == "alice"


def test_mcp_task_writes_store_the_username_and_blank_clears(mcp):
    task, added, _ = mcp
    vigil.add_case_task("c-1", "t", assignee="ALICE")
    assert added == ["alice"]
    vigil.update_case_task(5, assignee=" ")
    assert task.assignee is None


@pytest.mark.parametrize("sent", REFUSED + [""])
def test_mcp_escalation_to_a_non_person_writes_nothing(mcp, sent):
    _, _, escalated = mcp
    assert "error" in json.loads(vigil.escalate_case("c-1", sent, "why"))
    assert escalated == []


def test_mcp_escalation_stores_the_username(mcp):
    _, _, escalated = mcp
    vigil.escalate_case("c-1", "LEAD@example.com", "why")
    assert escalated == ["soc-lead"]
