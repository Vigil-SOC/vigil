"""Drafts an operator manifest from plain English (#80, #4024).

A draft is checked the way a PUT /api/v1/agents/{id} is before it is returned,
so the console never shows a manifest it could not save.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from core.agents.manager import CUSTOM_AGENT_ID_PREFIX
from core.agents.operator_manifest import (
    CheckUnavailable,
    ManifestRefused,
    read_manifest,
    vet_manifest,
)
from core.integrations.mcp.registry import MCPRegistry, safe_tool_names

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)
_ID_TAIL = re.compile(r"[^a-z0-9_-]+")
_ID_LINE = re.compile(r"^id:.*$", re.MULTILINE)

_EXAMPLE = """id: custom-phishing-triage
description: Triages reported phishing emails and says whether the sender is malicious.
needs:
  - case_records
  - indicator_lookup
skills:
  - phishing-analysis
system: |
  You are a SOC phishing analyst in the Vigil SOC platform.
  ...
conclusion: prose
max_turns: 12
"""


class AgentAIGenerator:
    """Generates / refines a draft operator manifest from natural language."""

    def __init__(self, mcp_registry: Optional[MCPRegistry] = None) -> None:
        self._mcp_registry = mcp_registry

    async def generate(
        self,
        description: str,
        current_draft: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """A draft manifest, or why there is none.

        Returns ``{"success", "manifest", "error", "refused"}``. ``refused`` is
        True when the model answered but the check would not store its draft.
        """
        if not description or not description.strip():
            return _failed("description is required")

        from core.llm.oneshot import NoProviderConfigured, complete

        try:
            raw = await complete(
                message=self._build_user_prompt(description, current_draft, feedback),
                system_prompt=self._build_system_prompt(),
                max_tokens=4096,
            )
        except NoProviderConfigured as e:
            return _failed(str(e))
        except Exception as e:
            logger.exception("Agent generation call failed")
            return _failed(str(e))
        if not raw:
            return _failed("Empty response from the model.")

        text = self._extract_manifest(raw)
        if text is None:
            return _failed("Could not read a manifest from the model's answer.")
        try:
            vet_manifest(text, self._mcp_registry)
        except ManifestRefused as e:
            return {**_failed(str(e)), "refused": True}
        except CheckUnavailable as e:
            logger.warning("could not check a drafted manifest: %s", e)
            return _failed("The agent service could not check the draft")
        return {"success": True, "manifest": text, "error": None, "refused": False}

    # --- Prompt building ---------------------------------------------------

    def _build_system_prompt(self) -> str:
        return (
            "You are a SOC agent designer for the Vigil SOC platform. Given a "
            "plain-English description of an agent's purpose, write the agent's "
            "operator manifest: one YAML document. Answer with the YAML in one "
            "```yaml fenced block and nothing else."
        )

    def _build_user_prompt(
        self,
        description: str,
        current_draft: Optional[str],
        feedback: Optional[str],
    ) -> str:
        parts: List[str] = [f"## Agent Purpose\n{description.strip()}"]
        if current_draft and current_draft.strip():
            parts.append(f"## Current Draft\n```yaml\n{current_draft.strip()}\n```")
            if feedback and feedback.strip():
                parts.append(
                    f"## Refinement Request\n{feedback.strip()}\n\n"
                    "Revise the draft to address the request. Keep its id and "
                    "what is already good; change only what the request asks for."
                )
        parts.extend(
            [
                f"## Existing Agents (do not duplicate)\n{self._agents_context()}",
                "## Capabilities (a need may name one; Vigil binds it to whatever "
                f"tool this deployment has for it)\n{self._capabilities_context()}",
                "## Tools (a need may also name one of these exactly)\n"
                f"{self._tools_context()}",
                f"## Skills (skills may name these only)\n{self._skills_context()}",
                (
                    "## Manifest fields\n"
                    f"- `id`: `{CUSTOM_AGENT_ID_PREFIX}` then lowercase letters, "
                    "digits, - or _.\n"
                    "- `description`: one line saying what the agent does.\n"
                    "- `system`: the agent's whole system prompt, as a `|` block: "
                    "its role, principles and numbered method.\n"
                    "- `needs`: 3-8 capabilities or tools from the lists above, "
                    "nothing else.\n"
                    "- `skills`: optional, names from the skills list only.\n"
                    "- `conclusion`: `prose`.\n"
                    "- `max_turns`: optional positive integer.\n"
                    "Leave out guard, record and min_results."
                ),
                f"## Example\n```yaml\n{_EXAMPLE}```",
            ]
        )
        return "\n\n".join(parts)

    def _agents_context(self) -> str:
        try:
            from core.agents.custom_agent_service import CustomAgentService
            from core.agents.manager import SOCAgentLibrary

            agents = {
                k: p.description for k, p in SOCAgentLibrary.get_all_agents().items()
            }
            agents.update(
                (r["id"], r["description"])
                for r in CustomAgentService().list_manifests()
            )
        except Exception as e:
            logger.warning(f"Could not load agent library: {e}")
            return "(agent library unavailable)"
        lines = [f"- `{k}`: {d}" for k, d in agents.items()]
        return "\n".join(lines) or "(no existing agents)"

    def _capabilities_context(self) -> str:
        from core.workflows.playbook_resolver import CAPABILITIES

        return ", ".join(f"`{c}`" for c in CAPABILITIES)

    def _tools_context(self) -> str:
        from core.llm.tool_schemas import ALL_TOOLS

        names = sorted(
            set(self._get_mcp_tool_names())
            | {t["name"] for t in ALL_TOOLS if t.get("name")}
        )
        return ", ".join(f"`{n}`" for n in names) or "(none)"

    def _skills_context(self) -> str:
        try:
            from core.skills.skill_library import load_skills, skill_roots

            skills = load_skills(skill_roots())
        except Exception as e:
            logger.warning(f"Could not load skills: {e}")
            return "(none)"
        lines = [f"- `{s.name}`: {s.description}" for s in skills]
        return "\n".join(lines) or "(none)"

    def _get_mcp_tool_names(self) -> List[str]:
        # Refreshed each draft, so a server connected since startup is offered.
        from core.integrations.mcp.registry import refresh_from_client

        registry = self._mcp_registry or MCPRegistry()
        try:
            refresh_from_client(registry)
        except Exception as e:
            logger.debug(f"MCP refresh failed: {e}")
        return safe_tool_names(registry)

    def _extract_manifest(self, raw: str) -> Optional[str]:
        """The model's YAML, kept as written unless its id needs the prefix."""
        fence = _FENCE.search(raw)
        text = (fence.group(1) if fence else raw).strip() + "\n"
        doc = read_manifest(text)
        if not doc:
            return None
        ident = str(doc.get("id") or "")
        if ident.startswith(CUSTOM_AGENT_ID_PREFIX):
            return text
        tail = ident.lower().removeprefix(CUSTOM_AGENT_ID_PREFIX)
        new_id = (
            f"{CUSTOM_AGENT_ID_PREFIX}{_ID_TAIL.sub('-', tail).strip('-') or 'agent'}"
        )
        # Only the id line changes, so the rest stays as the model wrote it.
        if "id" in doc:
            return _ID_LINE.sub(f"id: {new_id}", text, count=1)
        return f"id: {new_id}\n{text}"


def _failed(error: str) -> Dict[str, Any]:
    return {"success": False, "manifest": None, "error": error, "refused": False}
