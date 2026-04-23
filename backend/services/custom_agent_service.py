"""Service layer for custom SOC agents (Agent Builder)."""

import logging
import re
from typing import Any, Dict, List, Optional

from database.connection import get_db_manager
from database.models import CustomAgent
from services.soc_agents import CUSTOM_AGENT_ID_PREFIX, render_base_prompt

logger = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    slug = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return slug or "agent"


def build_agent_id(name: str) -> str:
    """Build a prefixed custom agent ID from a display name."""
    return f"{CUSTOM_AGENT_ID_PREFIX}{slugify(name)}"


class CustomAgentAlreadyExists(Exception):
    """Raised when attempting to create a custom agent whose ID already exists."""


class CustomAgentNotFound(Exception):
    """Raised when a custom agent is requested but does not exist."""


# Fields that can be updated via PATCH. Kept in sync with CustomAgent columns
# that belong to the editable agent definition (not audit fields).
UPDATABLE_FIELDS = {
    "name",
    "description",
    "icon",
    "color",
    "specialization",
    "role",
    "extra_principles",
    "methodology",
    "system_prompt_override",
    "recommended_tools",
    "max_tokens",
    "enable_thinking",
    "model",
}


class CustomAgentService:
    """CRUD service for custom SOC agents."""

    def list_agents(self) -> List[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            rows = (
                session.query(CustomAgent).order_by(CustomAgent.updated_at.desc()).all()
            )
            return [row.to_dict() for row in rows]

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            row = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            return row.to_dict() if row else None

    def get_effective_prompt(self, agent_row: Dict[str, Any]) -> str:
        """Return the system prompt that will actually be sent to Claude."""
        override = agent_row.get("system_prompt_override")
        if override:
            return override
        return render_base_prompt(
            role=agent_row.get("role", ""),
            extra_principles=agent_row.get("extra_principles", ""),
            methodology=agent_row.get("methodology", ""),
        )

    def create_agent(
        self,
        data: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        role = (data.get("role") or "").strip()
        if not role:
            raise ValueError("role is required")

        agent_id = build_agent_id(name)

        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            existing = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            if existing:
                raise CustomAgentAlreadyExists(
                    f"Custom agent already exists: {agent_id}"
                )

            kwargs = dict(
                id=agent_id,
                name=name,
                description=data.get("description"),
                icon=data.get("icon"),
                color=data.get("color"),
                specialization=data.get("specialization"),
                role=role,
                extra_principles=data.get("extra_principles") or "",
                methodology=data.get("methodology") or "",
                system_prompt_override=data.get("system_prompt_override"),
                recommended_tools=list(data.get("recommended_tools") or []),
                max_tokens=int(data.get("max_tokens") or 4096),
                enable_thinking=bool(data.get("enable_thinking") or False),
                model=data.get("model"),
                forked_from=data.get("forked_from"),
                created_by=created_by,
            )
            if data.get("component_category"):
                kwargs["component_category"] = data["component_category"]
            agent = CustomAgent(**kwargs)
            session.add(agent)
            session.flush()
            return agent.to_dict()

    def fork_from_profile(
        self,
        source_profile: Any,
        source_id: str,
        created_by: Optional[str] = None,
        new_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a custom agent by copying values from an ``AgentProfile``.

        Works for both built-in and custom source agents. The new row is
        an independent copy — the source is never touched. Appends "
        (copy)" to the name unless ``new_name`` is supplied, and bumps
        with a numeric suffix if the default ID collides.
        """
        base_name = new_name or f"{source_profile.name} (copy)"
        # Find an unused ID by appending " 2", " 3", ... on collision.
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            name = base_name
            counter = 2
            while True:
                candidate = build_agent_id(name)
                exists = (
                    session.query(CustomAgent)
                    .filter(CustomAgent.id == candidate)
                    .one_or_none()
                )
                if not exists:
                    break
                name = f"{base_name} {counter}"
                counter += 1

        # Extract fields off the AgentProfile. Some built-in fields
        # (extra_principles, methodology) aren't on the profile — we
        # capture the rendered system prompt as an override so the fork
        # behaves exactly like the source on day one.
        data = {
            "name": name,
            "role": getattr(source_profile, "role", "") or source_profile.name,
            "description": getattr(source_profile, "description", None),
            "icon": getattr(source_profile, "icon", None),
            "color": getattr(source_profile, "color", None),
            "specialization": getattr(source_profile, "specialization", None),
            "extra_principles": getattr(source_profile, "extra_principles", "") or "",
            "methodology": getattr(source_profile, "methodology", "") or "",
            "system_prompt_override": getattr(source_profile, "system_prompt", None),
            "recommended_tools": list(
                getattr(source_profile, "recommended_tools", []) or []
            ),
            "max_tokens": getattr(source_profile, "max_tokens", 4096),
            "enable_thinking": getattr(source_profile, "enable_thinking", False),
            "model": getattr(source_profile, "model", None),
            "component_category": getattr(source_profile, "component_category", None),
            "forked_from": source_id,
        }
        return self.create_agent(data, created_by=created_by)

    def update_agent(
        self,
        agent_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            if not agent:
                raise CustomAgentNotFound(agent_id)

            for key, value in updates.items():
                if key not in UPDATABLE_FIELDS:
                    continue
                if value is None and key in {"role"}:
                    # Required field cannot be set to null via PATCH
                    continue
                if key == "recommended_tools":
                    setattr(agent, key, list(value or []))
                elif key == "max_tokens":
                    setattr(agent, key, int(value))
                elif key == "enable_thinking":
                    setattr(agent, key, bool(value))
                else:
                    setattr(agent, key, value)

            session.flush()
            return agent.to_dict()

    def delete_agent(self, agent_id: str) -> bool:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            if not agent:
                return False
            session.delete(agent)
            return True
