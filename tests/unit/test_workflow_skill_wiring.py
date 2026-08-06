"""Unit tests for workflow execution seeing DB-backed skills as tools (#126).

Workflows used to execute through ``ClaudeService.run_agent_task`` which
drives the Claude Agent SDK. That path sees MCP tools only — our
``backend_tools`` layer (where ``skill_<slug>`` tools live) was
invisible, so workflows couldn't invoke user-authored skills.

The fix: ``execute_workflow`` drives ``ClaudeService.chat`` as an internal
engine primitive (skill tools refresh at the top of every invocation). As of
#413 4d-2 that dispatch is routed through ``LLMRouter.run_agent_chat`` — for the
Anthropic path (``_resolve_agent_provider`` returns ``None``) the router
constructs the same ``ClaudeService(use_backend_tools=True, use_agent_sdk=False,
…)`` and calls ``.chat``, so these tests still pin the construction + the
skill-tool threading contract, now with the gate + provider resolution mocked so
the oneshot path reaches the router deterministically without touching the DB.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from services.workflows_service import WorkflowDefinition, WorkflowsService

pytestmark = pytest.mark.unit


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


def _common_patches(stack: ExitStack):
    """Patch the Anthropic-key gate + provider resolution + run service so the
    oneshot path reaches ``run_agent_chat``'s Anthropic branch deterministically
    and without a live DB. ``_resolve_agent_provider`` returning ``None`` is the
    signal that keeps dispatch on ``ClaudeService.chat`` (vs the OpenAI loop)."""
    stack.enter_context(
        patch("services.llm_router.anthropic_api_key_available", return_value=True)
    )
    stack.enter_context(
        patch.object(
            WorkflowsService,
            "_resolve_agent_provider",
            return_value=(None, "claude-x"),
        )
    )
    run_svc = MagicMock()
    run_svc.begin_run.return_value = "run-1"
    run_svc.list_phases.return_value = []
    stack.enter_context(
        patch(
            "services.workflow_run_service.get_workflow_run_service",
            return_value=run_svc,
        )
    )


@pytest.mark.asyncio
async def test_execute_workflow_includes_skill_tools_in_allowed_list(monkeypatch):
    """Skill tool names from skill_tools_bridge should be threaded
    into the `recommended_tools` arg passed to chat()."""

    service = WorkflowsService()
    workflow = _make_workflow()
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: workflow)

    fake = _fake_claude_service()

    with ExitStack() as stack:
        _common_patches(stack)
        stack.enter_context(
            patch("services.claude_service.ClaudeService", return_value=fake)
        )
        stack.enter_context(
            patch(
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
            )
        )
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
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: workflow)

    fake = _fake_claude_service()
    with ExitStack() as stack:
        _common_patches(stack)
        stack.enter_context(
            patch("services.claude_service.ClaudeService", return_value=fake)
        )
        stack.enter_context(
            patch(
                "services.skill_tools_bridge.list_active_skill_tools",
                return_value=([], {}),
            )
        )
        result = await service.execute_workflow("wf-test", {})

    assert result["success"] is True
    assert result["skill_tools_available"] == []
    kwargs = fake.chat.call_args.kwargs
    assert "<available_skills>" not in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_execute_workflow_does_not_use_agent_sdk(monkeypatch):
    """Regression guard for #126: workflows must not take the Agent SDK
    path, because that branch never sees backend_tools + skills. The router's
    Anthropic path constructs ClaudeService with use_agent_sdk=False."""

    service = WorkflowsService()
    workflow = _make_workflow()
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: workflow)

    calls = []

    def _record_svc(**kwargs):
        calls.append(kwargs)
        return _fake_claude_service()

    with ExitStack() as stack:
        _common_patches(stack)
        stack.enter_context(
            patch("services.claude_service.ClaudeService", side_effect=_record_svc)
        )
        stack.enter_context(
            patch(
                "services.skill_tools_bridge.list_active_skill_tools",
                return_value=([], {}),
            )
        )
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
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: workflow)

    svc = MagicMock()
    svc.has_api_key.return_value = True
    svc.chat = MagicMock(side_effect=RuntimeError("boom"))

    with ExitStack() as stack:
        _common_patches(stack)
        stack.enter_context(
            patch("services.claude_service.ClaudeService", return_value=svc)
        )
        stack.enter_context(
            patch(
                "services.skill_tools_bridge.list_active_skill_tools",
                return_value=([], {}),
            )
        )
        result = await service.execute_workflow("wf-test", {})

    assert result["success"] is False
    assert "RuntimeError" in (result["error"] or "")
    assert "boom" in (result["error"] or "")
