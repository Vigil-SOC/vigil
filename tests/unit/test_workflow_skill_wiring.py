"""Unit tests for workflow execution seeing DB-backed skills as tools (#126).

Workflows used to execute through ``ClaudeService.run_agent_task`` which
drives the Claude Agent SDK. That path sees MCP tools only — our
``backend_tools`` layer (where ``skill_<slug>`` tools live) was
invisible, so workflows couldn't invoke user-authored skills.

The fix: ``execute_workflow`` now calls ``ClaudeService.chat`` as an
internal engine primitive. That entry point refreshes skill tools at
the top of every invocation. These tests lock in the contract that
skill tool names actually reach the executor + are mentioned in the
system prompt.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.workflows_service import WorkflowDefinition, WorkflowsService


def _make_workflow(agents=("investigator",), tools=("list_findings",)):
    return WorkflowDefinition(
        workflow_id="wf-test",
        file_path=None,
        metadata={
            "name": "Test Workflow",
            "description": "test",
            "agents": list(agents),
            "tools-used": list(tools),
            "use-case": "test",
            "trigger-examples": [],
        },
        body="Phase 1: investigate.\nPhase 2: report.\n",
        source="file",
    )


def _fake_claude_service(response_text: str = "done"):
    """MagicMock that satisfies the ClaudeService surface we call."""
    svc = MagicMock()
    svc.has_api_key.return_value = True
    svc.chat = MagicMock(return_value=response_text)
    return svc


@pytest.mark.asyncio
async def test_execute_workflow_includes_skill_tools_in_allowed_list(monkeypatch):
    """Skill tool names from skill_tools_bridge should be threaded
    into the `recommended_tools` arg passed to chat()."""

    service = WorkflowsService()
    workflow = _make_workflow()

    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: workflow
    )

    fake = _fake_claude_service()

    with patch(
        "services.claude_service.ClaudeService", return_value=fake
    ), patch(
        "services.skill_tools_bridge.list_active_skill_tools",
        return_value=(
            [
                {
                    "name": "skill_cookie_recipe_generator",
                    "description": "test",
                    "input_schema": {"type": "object"},
                }
            ],
            {},
        ),
    ):
        result = await service.execute_workflow("wf-test", {})

    assert result["success"] is True
    # ``skill_tools_available`` is surfaced on the response envelope so
    # the UI can tell the user which skills were in scope for this run.
    assert "skill_cookie_recipe_generator" in result["skill_tools_available"]

    # chat() was called once with recommended_tools containing the skill.
    assert fake.chat.call_count == 1
    kwargs = fake.chat.call_args.kwargs
    rec_tools = kwargs.get("recommended_tools") or []
    assert "skill_cookie_recipe_generator" in rec_tools
    # Plus the originally-declared workflow tools.
    assert "list_findings" in rec_tools
    # system_prompt names the skill so the model knows it's available.
    assert "skill_cookie_recipe_generator" in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_execute_workflow_no_skills_still_runs(monkeypatch):
    """Empty skill registry shouldn't break workflow execution or add
    an empty skills-hint block to the system prompt."""

    service = WorkflowsService()
    workflow = _make_workflow()
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: workflow
    )

    fake = _fake_claude_service()
    with patch(
        "services.claude_service.ClaudeService", return_value=fake
    ), patch(
        "services.skill_tools_bridge.list_active_skill_tools",
        return_value=([], {}),
    ):
        result = await service.execute_workflow("wf-test", {})

    assert result["success"] is True
    assert result["skill_tools_available"] == []
    kwargs = fake.chat.call_args.kwargs
    assert "<available_skills>" not in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_execute_workflow_does_not_use_agent_sdk(monkeypatch):
    """Regression guard for #126: workflows must not take the Agent SDK
    path, because that branch never sees backend_tools + skills."""

    service = WorkflowsService()
    workflow = _make_workflow()
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: workflow
    )

    calls = []

    def _record_svc(**kwargs):
        calls.append(kwargs)
        return _fake_claude_service()

    with patch(
        "services.claude_service.ClaudeService", side_effect=_record_svc
    ), patch(
        "services.skill_tools_bridge.list_active_skill_tools",
        return_value=([], {}),
    ):
        await service.execute_workflow("wf-test", {})

    assert len(calls) == 1
    assert calls[0].get("use_agent_sdk") is False
    # And backend_tools stays on (that's how skill tools load).
    assert calls[0].get("use_backend_tools") is True


@pytest.mark.asyncio
async def test_execute_workflow_surfaces_chat_exception_as_error(monkeypatch):
    """A raised exception from chat() should land as `success=False`
    with a readable error, not a 500 up the stack."""

    service = WorkflowsService()
    workflow = _make_workflow()
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: workflow
    )

    svc = MagicMock()
    svc.has_api_key.return_value = True
    svc.chat = MagicMock(side_effect=RuntimeError("boom"))

    with patch(
        "services.claude_service.ClaudeService", return_value=svc
    ), patch(
        "services.skill_tools_bridge.list_active_skill_tools",
        return_value=([], {}),
    ):
        result = await service.execute_workflow("wf-test", {})

    assert result["success"] is False
    assert "RuntimeError" in (result["error"] or "")
    assert "boom" in (result["error"] or "")
