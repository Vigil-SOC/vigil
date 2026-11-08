"""
AI-assisted workflow generator.

Takes a natural-language description of a security scenario and produces a
draft workflow definition (phases and the agent each runs) by prompting the
assigned model with context about the available agents and existing workflow
patterns. A phase's tools come with its agent, so a draft names none.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from core.workflows.workflows_service import WorkflowsService

logger = logging.getLogger(__name__)


class WorkflowAIGenerator:
    """Generates draft workflow definitions from natural-language descriptions."""

    def __init__(self, workflows: Optional[WorkflowsService] = None):
        self._workflows = workflows

    async def generate(self, description: str) -> Dict[str, Any]:
        """
        Generate a draft workflow from a natural-language description.

        Args:
            description: Plain-English scenario (e.g.,
                "Investigate suspicious login and contain the account if malicious").

        Returns:
            {
                "success": bool,
                "draft": {...workflow dict...} | None,
                "error": str | None,
                "raw": str  # raw model response, for debugging
            }
        """
        if not description or not description.strip():
            return {
                "success": False,
                "draft": None,
                "error": "description is required",
                "raw": "",
            }

        from core.llm.oneshot import NoProviderConfigured, complete

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(description)

        try:
            raw = await complete(
                message=user_prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
            )
        except NoProviderConfigured as e:
            return {"success": False, "draft": None, "error": str(e), "raw": ""}
        except Exception as e:
            logger.exception("Workflow generation call failed")
            return {"success": False, "draft": None, "error": str(e), "raw": ""}

        if not raw:
            return {
                "success": False,
                "draft": None,
                "error": "Empty response from the model.",
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
            "that uses only the agents listed in the user message. "
            "Return STRICT JSON only \u2014 no prose, no markdown, no code fences."
        )

    def _build_user_prompt(self, description: str) -> str:
        agents_block = self._agents_context()
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
                    "steps": ["Step 1", "Step 2"],
                    "expected_output": "Description of phase output",
                    "approval_required": False,
                }
            ],
        }

        return (
            f"## Scenario\n{description.strip()}\n\n"
            f"## Available Agents\n{agents_block}\n\n"
            f"## Existing Workflow Patterns (for reference)\n{exemplars_block}\n\n"
            "## Requirements\n"
            "- Use ONLY the agent_ids listed above.\n"
            "- Prefer 3-5 phases unless the scenario is trivial.\n"
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
            from core.agents.enablement import disabled_agent_ids
            from core.agents.manager import SOCAgentLibrary

            disabled = disabled_agent_ids()
            agents = {
                k: v
                for k, v in SOCAgentLibrary.get_all_agents().items()
                if k not in disabled
            }
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
        """Extract the first valid JSON object from the response."""
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
        """Fill in defaults and make the draft safe to save as-is."""
        phases = draft.get("phases") or []
        for idx, phase in enumerate(phases, start=1):
            phase.setdefault("phase_id", f"phase-{idx}")
            phase["order"] = idx
            # An agent brings its own tools; a list the model adds anyway is not kept.
            phase.pop("tools", None)
            phase.setdefault("steps", [])
            phase.setdefault("approval_required", False)

        return {
            "name": draft.get("name", "Untitled Workflow"),
            "description": draft.get("description", ""),
            "use_case": draft.get("use_case", ""),
            "trigger_examples": draft.get("trigger_examples") or [],
            "phases": phases,
        }
