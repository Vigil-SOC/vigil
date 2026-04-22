"""
AI-assisted custom SOC agent generator (issue #80, Phase 2).

Takes a natural-language description of an agent's purpose — and optionally a
current draft plus user feedback — and produces a draft agent configuration by
prompting Claude with context about the platform's existing agents, available
MCP tools, and the shape of the Vigil base prompt.

Routes through ClaudeService, which flows through Bifrost per GH #84.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentAIGenerator:
    """Generates / refines draft custom agent configurations from natural language."""

    def __init__(self) -> None:
        self._mcp_tool_names_cache: Optional[List[str]] = None

    async def generate(
        self,
        description: str,
        current_draft: Optional[Dict[str, Any]] = None,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate or refine a draft agent config.

        - First call: pass ``description`` only.
        - Refinement: pass the prior ``current_draft`` plus ``feedback`` describing
          the changes the user wants. ``description`` carries forward so the model
          keeps the original intent in view.

        Returns:
            {
                "success": bool,
                "draft": {...agent config...} | None,
                "error": str | None,
                "raw": str,  # raw model response, for debugging
            }
        """
        if not description or not description.strip():
            return {
                "success": False,
                "draft": None,
                "error": "description is required",
                "raw": "",
            }

        from services.claude_service import ClaudeService

        claude = ClaudeService(use_backend_tools=False, use_mcp_tools=False)
        if not claude.has_api_key():
            return {
                "success": False,
                "draft": None,
                "error": "Claude API is not configured.",
                "raw": "",
            }

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(description, current_draft, feedback)

        try:
            raw = claude.chat(
                message=user_prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
                enable_thinking=False,
            )
        except Exception as e:
            logger.exception("Agent generation call failed")
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
                "error": "Could not parse agent JSON from model response.",
                "raw": raw,
            }

        normalized = self._normalize_draft(draft)
        return {"success": True, "draft": normalized, "error": None, "raw": raw}

    # --- Prompt building ---------------------------------------------------

    def _build_system_prompt(self) -> str:
        return (
            "You are a SOC agent designer for the Vigil SOC platform. "
            "Given a plain-English description of an agent's purpose, design a "
            "specialized SOC agent by writing its role, extra principles, "
            "methodology, and a list of recommended MCP tools drawn only from "
            "the provided catalog. Return STRICT JSON only \u2014 no prose, "
            "no markdown, no code fences."
        )

    def _build_user_prompt(
        self,
        description: str,
        current_draft: Optional[Dict[str, Any]],
        feedback: Optional[str],
    ) -> str:
        agents_block = self._agents_context()
        tools_block = self._tools_context()
        base_prompt_shape = self._base_prompt_shape()

        schema = {
            "name": "Short Title Case agent name",
            "description": "One-line description of what the agent does",
            "specialization": "Short specialization area, e.g. 'Phishing Analysis'",
            "icon": "Single uppercase letter (e.g. 'P')",
            "color": "Hex color like '#8e44ad'",
            "role": (
                "Short role phrase for BASE_PROMPT "
                "(e.g. 'phishing specialist')"
            ),
            "extra_principles": (
                "Bullet list of additional principles, one per line prefixed "
                "with '- '. Rendered inside <principles>."
            ),
            "methodology": (
                "Numbered methodology (1., 2., ...) describing how the agent "
                "operates. Rendered after the principles block."
            ),
            "recommended_tools": ["tool_name_1", "tool_name_2"],
            "max_tokens": 4096,
            "enable_thinking": False,
        }

        parts: List[str] = [f"## Agent Purpose\n{description.strip()}"]

        if current_draft and feedback and feedback.strip():
            parts.append(
                "## Current Draft\n"
                "```json\n"
                f"{json.dumps(current_draft, indent=2)}\n"
                "```\n\n"
                "## Refinement Request\n"
                f"{feedback.strip()}\n\n"
                "Revise the draft to address the refinement request. Keep what is "
                "already good; change only what the request asks for."
            )
        elif current_draft:
            parts.append(
                "## Current Draft (for context)\n"
                "```json\n"
                f"{json.dumps(current_draft, indent=2)}\n"
                "```"
            )

        parts.extend(
            [
                f"## Existing Agents (do not duplicate)\n{agents_block}",
                f"## Available MCP Tools (choose only from this list)\n{tools_block}",
                (
                    "## Base Prompt Shape\n"
                    "Your `role`, `extra_principles`, and `methodology` fields are "
                    "rendered into this template (Vigil preserves the "
                    "entity-recognition and memory-palace directives):\n\n"
                    f"{base_prompt_shape}"
                ),
                (
                    "## Requirements\n"
                    "- `role` is a short noun phrase. It renders as "
                    "\"You are a SOC {role} in the Vigil SOC platform.\"\n"
                    "- `extra_principles` is added AFTER Vigil's baseline "
                    "principles. Use '- ' bullets, one per line.\n"
                    "- `methodology` is a numbered step list (1., 2., 3., ...) "
                    "describing how the agent should operate end-to-end.\n"
                    "- `recommended_tools` MUST be chosen from the Available MCP "
                    "Tools list above. Prefer 3-8 tools.\n"
                    "- `icon` is exactly ONE uppercase letter.\n"
                    "- `color` is a hex color like '#8e44ad'.\n"
                    "- `max_tokens` between 2048 and 16384 (4096 is a reasonable "
                    "default).\n"
                    "- `enable_thinking` should be true ONLY for agents that need "
                    "deep multi-step reasoning (e.g. forensics, correlation)."
                ),
                (
                    "## Output Schema\n"
                    "Return ONE JSON object matching this schema exactly:\n"
                    f"{json.dumps(schema, indent=2)}"
                ),
            ]
        )

        return "\n\n".join(parts)

    def _agents_context(self) -> str:
        try:
            from services.soc_agents import SOCAgentLibrary

            agents = SOCAgentLibrary.get_all_agents()
        except Exception as e:
            logger.warning(f"Could not load agent library: {e}")
            return "(agent library unavailable)"

        lines: List[str] = []
        for agent_id, profile in agents.items():
            lines.append(
                f"- `{agent_id}` \u2014 {profile.name}: "
                f"{profile.specialization or profile.description}"
            )
        return "\n".join(lines) or "(no existing agents)"

    def _tools_context(self) -> str:
        tool_names = self._get_mcp_tool_names()
        if not tool_names:
            return (
                "(MCP registry unavailable; leave `recommended_tools` empty "
                "and the user can add tools manually)"
            )
        # Group by server prefix so the model sees the shape of each integration.
        grouped: Dict[str, List[str]] = {}
        for name in sorted(tool_names):
            server = name.split("_", 1)[0] if "_" in name else "other"
            grouped.setdefault(server, []).append(name)
        lines: List[str] = []
        for server, names in grouped.items():
            lines.append(f"- **{server}**: {', '.join(names)}")
        return "\n".join(lines)

    def _get_mcp_tool_names(self) -> List[str]:
        if self._mcp_tool_names_cache is not None:
            return self._mcp_tool_names_cache
        try:
            from services.mcp_registry import get_mcp_registry

            registry = get_mcp_registry()
            names = list(registry.get_tool_names() or [])
        except Exception as e:
            logger.debug(f"MCP registry unavailable: {e}")
            names = []
        self._mcp_tool_names_cache = names
        return names

    def _base_prompt_shape(self) -> str:
        return (
            "You are a SOC {role} in the Vigil SOC platform.\n"
            "\n"
            "<entity_recognition> ... </entity_recognition>  (preserved by Vigil)\n"
            "<available_tools> ... </available_tools>       (preserved by Vigil)\n"
            "<memory_operations> ... </memory_operations>   (preserved by Vigil)\n"
            "\n"
            "<principles>\n"
            "- Always fetch data via tools before analyzing\n"
            "- Be evidence-based and document reasoning\n"
            "- Use parallel tool calls for independent queries\n"
            "{extra_principles}\n"
            "</principles>\n"
            "\n"
            "{methodology}"
        )

    # --- Response parsing --------------------------------------------------

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract the first valid JSON object from the response."""
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        else:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            candidate = match.group(0) if match else text
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    def _normalize_draft(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in defaults and sanitize values so the draft is safe to save."""
        name = (draft.get("name") or "").strip() or "Untitled Agent"
        icon_raw = (draft.get("icon") or "C").strip()
        icon = (icon_raw[:1] or "C").upper()
        color = (draft.get("color") or "#888888").strip()
        if not re.match(r"^#[0-9A-Fa-f]{6}$", color):
            color = "#888888"

        try:
            max_tokens = int(draft.get("max_tokens") or 4096)
        except (TypeError, ValueError):
            max_tokens = 4096
        max_tokens = max(2048, min(16384, max_tokens))

        tools_raw = draft.get("recommended_tools") or []
        tools = [str(t).strip() for t in tools_raw if str(t).strip()]
        # Deduplicate while preserving order.
        seen: set = set()
        tools = [t for t in tools if not (t in seen or seen.add(t))]

        return {
            "name": name,
            "description": (draft.get("description") or "").strip(),
            "specialization": (draft.get("specialization") or "").strip(),
            "icon": icon,
            "color": color,
            "role": (draft.get("role") or "").strip(),
            "extra_principles": (draft.get("extra_principles") or "").strip(),
            "methodology": (draft.get("methodology") or "").strip(),
            "recommended_tools": tools,
            "max_tokens": max_tokens,
            "enable_thinking": bool(draft.get("enable_thinking") or False),
        }


_generator: Optional[AgentAIGenerator] = None


def get_agent_ai_generator() -> AgentAIGenerator:
    """Return the singleton AgentAIGenerator instance."""
    global _generator
    if _generator is None:
        _generator = AgentAIGenerator()
    return _generator
