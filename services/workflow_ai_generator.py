import json
import logging
import re
from typing import Any, Dict, List, Optional

from services.mcp_registry import MCPRegistry
from services.workflows_service import WorkflowsService

logger = logging.getLogger(__name__)


# Turns a natural-language security scenario into a draft workflow definition
# (phases, agents, tools) by prompting Claude with the available context.
class WorkflowAIGenerator:
    def __init__(
        self,
        workflows: Optional[WorkflowsService] = None,
        mcp_registry: Optional[MCPRegistry] = None,
    ):
        self._workflows = workflows
        self._mcp_registry = mcp_registry
        self._mcp_tool_names_cache: Optional[List[str]] = None

    async def generate(self, description: str) -> Dict[str, Any]:
        if not description or not description.strip():
            return {
                "success": False,
                "draft": None,
                "error": "description is required",
                "raw": "",
            }

        from core.llm.harness.claude import ClaudeService

        claude = ClaudeService(use_backend_tools=False, use_mcp_tools=False)
        if not claude.has_api_key():
            return {
                "success": False,
                "draft": None,
                "error": "Claude API is not configured.",
                "raw": "",
            }

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(description)

        try:
            raw = claude.chat(
                message=user_prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
                enable_thinking=False,
            )
        except Exception as e:
            logger.exception("Workflow generation call failed")
            return {"success": False, "draft": None, "error": str(e), "raw": ""}

        if not raw:
            return {
                "success": False,
                "draft": None,
                "error": "Empty response from Claude.",
                "raw": "",
            }

        draft = self._extract_json(raw)
        if not draft:
            return {
                "success": False,
                "draft": None,
                "error": "Could not parse workflow JSON from model response.",
                "raw": raw,
            }

        normalized = self._normalize_draft(draft)
        return {"success": True, "draft": normalized, "error": None, "raw": raw}

    # --- Prompt building ---------------------------------------------------

    def _build_system_prompt(self) -> str:
        return (
            "You are a SOC workflow designer for the Vigil SOC platform. "
            "Given a plain-English scenario, design a multi-phase agent workflow "
            "that uses only the agents and tools listed in the user message. "
            "Return STRICT JSON only \u2014 no prose, no markdown, no code fences."
        )

    def _build_user_prompt(self, description: str) -> str:
        agents_block = self._agents_context()
        tools_block = self._tools_context()
        exemplars_block = self._exemplars_context()

        schema = {
            "name": "Short Title Case name for the workflow",
            "description": "One-line description of what this workflow does",
            "use_case": "When to trigger this workflow",
            "trigger_examples": ["Example invocation 1", "Example invocation 2"],
            "phases": [
                {
                    "phase_id": "phase-1",
                    "order": 1,
                    "agent_id": "triage",
                    "name": "Phase name",
                    "purpose": "What this phase accomplishes",
                    "tools": ["tool_name_1", "tool_name_2"],
                    "steps": ["Step 1", "Step 2"],
                    "expected_output": "Description of phase output",
                    "timeout_seconds": 300,
                    "approval_required": False,
                }
            ],
        }

        return (
            f"## Scenario\n{description.strip()}\n\n"
            f"## Available Agents\n{agents_block}\n\n"
            f"## Available Tools\n{tools_block}\n\n"
            f"## Existing Workflow Patterns (for reference)\n{exemplars_block}\n\n"
            "## Requirements\n"
            "- Use ONLY the agent_ids listed above.\n"
            "- Prefer 3-5 phases unless the scenario is trivial.\n"
            "- Each phase's `tools` must be chosen from the available tool list.\n"
            "- `phase_id` values must be unique (e.g., phase-1, phase-2).\n"
            "- `order` must start at 1 and increase by 1.\n"
            "- `approval_required` should be true for any containment "
            "or destructive phase.\n\n"
            "## Output Schema\n"
            "Return ONE JSON object matching this schema exactly:\n"
            f"{json.dumps(schema, indent=2)}"
        )

    def _agents_context(self) -> str:
        try:
            from core.agents.manager import SOCAgentLibrary

            agents = SOCAgentLibrary.get_all_agents()
        except Exception as e:
            logger.warning(f"Could not load agent library: {e}")
            return "(agent library unavailable)"

        lines = []
        for agent_id, profile in agents.items():
            tools = ", ".join(profile.recommended_tools[:6]) or "n/a"
            lines.append(
                f"- `{agent_id}` \u2014 {profile.name}: {profile.specialization}. "
                f"Typical tools: {tools}."
            )
        return "\n".join(lines)

    def _tools_context(self) -> str:
        tool_names = self._get_mcp_tool_names()
        if not tool_names:
            return (
                "(MCP registry unavailable; use tool names from the existing "
                "workflow patterns)"
            )
        return ", ".join(sorted(tool_names)[:80])

    def _get_mcp_tool_names(self) -> List[str]:
        if self._mcp_tool_names_cache is not None:
            return self._mcp_tool_names_cache
        try:
            registry = self._mcp_registry or MCPRegistry()
            names = list(registry.get_tool_names() or [])
        except Exception as e:
            logger.debug(f"MCP registry unavailable: {e}")
            names = []
        self._mcp_tool_names_cache = names
        return names

    def _exemplars_context(self) -> str:
        try:
            service = self._workflows or WorkflowsService()
            workflows = service.list_workflows()
        except Exception as e:
            logger.debug(f"Could not load existing workflows: {e}")
            return "(no existing workflows available)"

        if not workflows:
            return "(no existing workflows available)"

        lines = []
        for wf in workflows[:4]:
            agents = ", ".join(wf.get("agents", []) or [])
            lines.append(
                f"- **{wf.get('name')}** ({wf.get('id')}): {wf.get('description')}. "
                f"Agents: {agents}."
            )
        return "\n".join(lines)

    # --- Response parsing --------------------------------------------------

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        # Strip ```json ... ``` fences if present
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        else:
            # Fall back to the first {...} block
            match = re.search(r"\{.*\}", text, re.DOTALL)
            candidate = match.group(0) if match else text

        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    def _normalize_draft(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        phases = draft.get("phases") or []
        for idx, phase in enumerate(phases, start=1):
            phase.setdefault("phase_id", f"phase-{idx}")
            phase["order"] = idx
            phase.setdefault("tools", [])
            phase.setdefault("steps", [])
            phase.setdefault("timeout_seconds", 300)
            phase.setdefault("approval_required", False)
            phase.setdefault("conditions", None)
            phase.setdefault("parallel_group", None)

        return {
            "name": draft.get("name", "Untitled Workflow"),
            "description": draft.get("description", ""),
            "use_case": draft.get("use_case", ""),
            "trigger_examples": draft.get("trigger_examples") or [],
            "phases": phases,
            "graph_layout": {},
        }
