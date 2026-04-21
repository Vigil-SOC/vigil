"""Service for the Skill Builder (Issue #82).

A Skill is a reusable, parameterized SOC capability — think "Detect lateral
movement via RDP" or "Full IOC enrichment" — that agents and workflows will
compose in future PRs. This service owns:

  * CRUD persistence against the ``skills`` table
  * AI-assisted generation of new skills via ClaudeService.chat, mirroring the
    multi-turn clarification flow used by CustomIntegrationService

Execution of skills is out of scope for the MVP and will be added as a separate
ARQ-backed worker (see llm_worker.py for the pattern).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.connection import get_db_manager
from database.models import Skill

logger = logging.getLogger(__name__)


# Standard MITRE ATT&CK tactics. Injected into the skill-generation prompt so
# Claude can reference them for detection/response skill design. Kept in-file
# because the full taxonomy JSON isn't shipped with the repo.
MITRE_TACTICS = [
    ("TA0001", "Initial Access"),
    ("TA0002", "Execution"),
    ("TA0003", "Persistence"),
    ("TA0004", "Privilege Escalation"),
    ("TA0005", "Defense Evasion"),
    ("TA0006", "Credential Access"),
    ("TA0007", "Discovery"),
    ("TA0008", "Lateral Movement"),
    ("TA0009", "Collection"),
    ("TA0010", "Exfiltration"),
    ("TA0011", "Command and Control"),
    ("TA0040", "Impact"),
]


class SkillService:
    """Persistence + AI generation for Skills."""

    # ------------------------------------------------------------------ CRUD

    def list_skills(
        self,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Return all skills, optionally filtered by category / is_active."""
        with get_db_manager().session_scope() as session:
            q = session.query(Skill)
            if category:
                q = q.filter(Skill.category == category)
            if is_active is not None:
                q = q.filter(Skill.is_active == is_active)
            q = q.order_by(Skill.created_at.desc())
            return [s.to_dict() for s in q.all()]

    def get_skill(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one skill by id, or None."""
        with get_db_manager().session_scope() as session:
            row = session.get(Skill, skill_id)
            return row.to_dict() if row else None

    def create_skill(
        self,
        data: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a new skill and return its dict form."""
        with get_db_manager().session_scope() as session:
            skill = Skill(
                skill_id=Skill.generate_skill_id(),
                name=data["name"],
                description=data.get("description"),
                category=data["category"],
                input_schema=data.get("input_schema") or {},
                output_schema=data.get("output_schema") or {},
                required_tools=data.get("required_tools") or [],
                prompt_template=data["prompt_template"],
                execution_steps=data.get("execution_steps") or [],
                is_active=data.get("is_active", True),
                created_by=created_by or data.get("created_by"),
                version=1,
            )
            session.add(skill)
            session.flush()
            return skill.to_dict()

    def update_skill(
        self,
        skill_id: str,
        patch: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Apply a partial update, bumping ``version`` on any content change."""
        content_fields = {
            "name",
            "description",
            "category",
            "input_schema",
            "output_schema",
            "required_tools",
            "prompt_template",
            "execution_steps",
        }
        with get_db_manager().session_scope() as session:
            row = session.get(Skill, skill_id)
            if not row:
                return None
            bumped = False
            for key, value in patch.items():
                if value is None:
                    continue
                if not hasattr(row, key):
                    continue
                if key in content_fields and getattr(row, key) != value:
                    bumped = True
                setattr(row, key, value)
            if bumped:
                row.version = (row.version or 1) + 1
            session.flush()
            return row.to_dict()

    def delete_skill(self, skill_id: str) -> bool:
        """Hard-delete a skill. Returns True on success, False if not found."""
        with get_db_manager().session_scope() as session:
            row = session.get(Skill, skill_id)
            if not row:
                return False
            session.delete(row)
            return True

    # ------------------------------------------------------------------ AI

    async def generate_skill(
        self,
        description: str,
        category: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Generate a skill draft from a natural-language description.

        Supports multi-turn clarification: if Claude replies starting with
        "I have some questions:" the caller should show the question, collect
        an answer, and re-invoke with the accumulated conversation_history.
        """
        try:
            from services.claude_service import ClaudeService

            claude = ClaudeService(use_mcp_tools=False)
            if not claude.has_api_key():
                return {
                    "success": False,
                    "error": (
                        "Claude API is not configured. "
                        "Please configure it in Settings."
                    ),
                }

            system_prompt = self._system_prompt()

            if conversation_history:
                last_message = conversation_history[-1]["content"]
                context = (
                    conversation_history[:-1] if len(conversation_history) > 1 else None
                )
                response = claude.chat(
                    message=last_message,
                    context=context,
                    system_prompt=system_prompt,
                )
            else:
                initial = self._initial_user_prompt(description, category)
                response = claude.chat(
                    message=initial,
                    system_prompt=system_prompt,
                )

            if response is None:
                return {"success": False, "error": "Claude returned no response."}

            if self._is_asking_questions(response):
                if conversation_history:
                    full_history = conversation_history + [
                        {"role": "assistant", "content": response}
                    ]
                else:
                    full_history = [
                        {
                            "role": "user",
                            "content": self._initial_user_prompt(description, category),
                        },
                        {"role": "assistant", "content": response},
                    ]
                return {
                    "success": True,
                    "needs_clarification": True,
                    "message": response,
                    "conversation_history": full_history,
                }

            skill_data = self._parse_claude_response(response)
            if not skill_data:
                return {
                    "success": False,
                    "error": (
                        "Failed to parse Claude's response. "
                        "Try rephrasing the description."
                    ),
                }

            # Allow the caller-specified category to take precedence when present.
            if category:
                skill_data["category"] = category

            return {
                "success": True,
                "needs_clarification": False,
                "skill": skill_data,
                "message": f"Generated skill '{skill_data.get('name', 'Untitled')}'",
            }

        except Exception as e:  # pragma: no cover - defensive
            logger.error("Error generating skill: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    # ---------------------------------------------------------- prompt parts

    def _system_prompt(self) -> str:
        """Build the system prompt with dynamic context.

        Injects available MCP tools, MITRE tactics, and existing skills.
        """
        tools_block = self._format_mcp_tools(self._get_available_mcp_tools())
        mitre_block = "\n".join(f"  {tid} — {name}" for tid, name in MITRE_TACTICS)
        existing_block = self._format_existing_skills()

        return f"""You design reusable "Skills" for the Vigil SOC platform.

A Skill is a parameterized, composable unit of work an AI SOC analyst can
invoke. Each skill has: name, category, input_schema (JSON Schema), output_schema,
required_tools (chosen from the MCP tool list below), prompt_template (may
reference inputs with {{{{param}}}} placeholders), and an ordered execution_steps
list.

AVAILABLE MCP TOOLS
{tools_block}

MITRE ATT&CK TACTICS (for detection/response skills)
{mitre_block}

EXISTING SKILLS (do not duplicate; composition is a future feature)
{existing_block}

OUTPUT CONTRACT
Respond with JSON only, in a ```json fenced block, matching this shape:
```json
{{
  "name": "short human-readable name",
  "description": "1-2 sentence summary",
  "category": "detection|enrichment|response|reporting|custom",
  "input_schema": {{
    "type": "object",
    "properties": {{
      "param_name": {{"type": "string", "description": "..."}}
    }},
    "required": ["param_name"]
  }},
  "output_schema": {{"type": "object", "properties": {{...}}}},
  "required_tools": ["server.tool_name", "..."],
  "prompt_template": "Instructions for the LLM, referencing {{{{param_name}}}}.",
  "execution_steps": [
    {{"step_id": "1", "type": "mcp_tool_call", "tool": "server.tool_name",
      "input_mapping": {{"arg": "{{{{param_name}}}}"}}, "output_key": "step1_result"}}
  ]
}}
```

CLARIFICATION RULE
If the requested capability is ambiguous (unclear data source, scope, or
output), reply starting with "I have some questions:" and list your questions.
Do NOT emit the JSON until you have the information you need.
"""

    def _initial_user_prompt(self, description: str, category: Optional[str]) -> str:
        cat_hint = f"\nCategory: {category}" if category else ""
        return (
            f"Design a skill for the following capability:\n\n"
            f"{description.strip()}{cat_hint}\n\n"
            "Return the JSON skill definition, or ask clarifying "
            "questions if anything is ambiguous."
        )

    # ---------------------------------------------------------- response parsing

    def _is_asking_questions(self, response: str) -> bool:
        """Detect clarification requests; mirrors custom_integration_service logic."""
        indicators = [
            "I have some questions:",
            "Could you clarify",
            "I need more information",
            "Can you provide",
            "Which data source",
            "Before I generate",
        ]
        has_questions = any(i.lower() in response.lower() for i in indicators)
        has_json = (
            "```json" in response
            or '"required_tools"' in response
            or '"input_schema"' in response
        )
        return has_questions and not has_json

    def _parse_claude_response(self, response: str) -> Optional[Dict[str, Any]]:
        """Extract and validate a skill-shaped JSON object from Claude's response."""
        try:
            json_str = self._extract_json(response)
            if not json_str:
                logger.error("No JSON block found in Claude's skill response")
                return None
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse skill JSON: %s", e)
            return None

        required = {"name", "category", "required_tools", "prompt_template"}
        missing = required - set(data.keys())
        if missing:
            logger.error("Skill JSON missing required keys: %s", missing)
            return None

        # Normalize optional collection fields so the Pydantic response model
        # and DB defaults stay consistent.
        data.setdefault("description", "")
        data.setdefault("input_schema", {})
        data.setdefault("output_schema", {})
        data.setdefault("execution_steps", [])
        data.setdefault("is_active", True)
        if not isinstance(data.get("required_tools"), list):
            data["required_tools"] = []
        return data

    @staticmethod
    def _extract_json(response: str) -> Optional[str]:
        # Prefer fenced blocks — Claude is instructed to use them.
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            return match.group(1)
        # Fall back to the first balanced JSON object in the text. We look for a
        # top-level object that mentions required_tools so we don't grab an
        # unrelated snippet from the LLM's commentary.
        match = re.search(r"\{[^{}]*\"required_tools\"[\s\S]*\}", response)
        return match.group(0) if match else None

    # ---------------------------------------------------------- context helpers

    def _get_available_mcp_tools(self) -> List[Dict[str, str]]:
        """Read mcp-config.json and return a list of {server, description}.

        We don't spin up MCP servers here (too heavy for a prompt builder);
        instead we surface server names + any short description Claude can
        use to pick reasonable tools. The actual tool names per server are
        filled in by Claude from knowledge + the server name.
        """
        try:
            project_root = Path(__file__).resolve().parent.parent
            config_path = project_root / "mcp-config.json"
            if not config_path.exists():
                return []
            with open(config_path, "r") as f:
                config = json.load(f)
            servers = config.get("mcpServers", {})
            tools: List[Dict[str, str]] = []
            for name, spec in servers.items():
                if name.startswith("_comment"):
                    continue
                desc = spec.get("description") or spec.get("_note") or ""
                tools.append({"server": name, "description": str(desc)})
            return tools
        except Exception as e:
            logger.warning("Could not load mcp-config.json for skill prompt: %s", e)
            return []

    @staticmethod
    def _format_mcp_tools(tools: List[Dict[str, str]]) -> str:
        if not tools:
            return "  (none discovered; use generic MCP tool names)"
        lines = []
        for t in tools:
            line = f"  - {t['server']}"
            if t.get("description"):
                line += f" — {t['description']}"
            lines.append(line)
        return "\n".join(lines)

    def _format_existing_skills(self) -> str:
        try:
            skills = self.list_skills(is_active=True)
        except Exception as e:
            logger.debug("Could not list existing skills for prompt: %s", e)
            return "  (none)"
        if not skills:
            return "  (none)"
        return "\n".join(
            f"  - [{s['category']}] {s['name']}: {(s.get('description') or '')[:80]}"
            for s in skills[:25]
        )
