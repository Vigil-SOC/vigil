"""Service layer for custom SOC agents (Agent Builder)."""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from core.agents.builtins import blank_model
from core.agents.manager import CUSTOM_AGENT_ID_PREFIX
from core.agents.operator_manifest import (
    is_legacy_shaped,
    manifest_from_row,
    manifest_needs,
    read_manifest,
)
from core.agents.prompts import prompt_for_row
from core.auth import principals
from core.storage.config_service import write_config_audit
from core.storage.connection import get_db_manager
from core.storage.models import CustomAgent
from core.storage.schemas import CustomAgentSchema

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
    "fallback_model",
}


_EMPTY_MODELS = {"model": None, "fallback_model": None}


def model_pair(model: Any, fallback: Any) -> Dict[str, Optional[str]]:
    """The two model strings, with blanks stored as null."""
    return {
        "model": blank_model(model),
        "fallback_model": blank_model(fallback),
    }


def model_strings_changed(
    before: Optional[Dict[str, Optional[str]]],
    after: Optional[Dict[str, Optional[str]]],
) -> bool:
    """Whether either model string differs. A missing pair is both unset."""
    return (before or _EMPTY_MODELS) != (after or _EMPTY_MODELS)


def _audit_model_strings(
    session: Any,
    agent_id: str,
    action: str,
    before: Optional[Dict[str, Optional[str]]],
    after: Optional[Dict[str, Optional[str]]],
    changed_by: Optional[str],
) -> None:
    """One audit entry on this session when either string changes."""
    if not changed_by or not model_strings_changed(before, after):
        return
    write_config_audit(
        session,
        config_type="custom_agent",
        config_key=agent_id,
        action=action,
        old_value=before,
        new_value=after,
        changed_by=changed_by,
    )


# One entry per manifest change, on the caller's session so it commits with it.
def _audit_manifest(
    session: Any,
    agent_id: str,
    action: str,
    before: Optional[str],
    after: Optional[str],
    changed_by: Optional[str],
) -> None:
    if not changed_by or before == after:
        return
    write_config_audit(
        session,
        config_type="custom_agent",
        config_key=agent_id,
        action=action,
        old_value=None if before is None else {"manifest": before},
        new_value=None if after is None else {"manifest": after},
        changed_by=changed_by,
    )


class ManifestNotLegacy(ValueError):
    """The stored manifest says more than the persona columns can carry."""


class CustomAgentService:
    """CRUD service for custom SOC agents."""

    def list_agents(self) -> List[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            rows = (
                session.query(CustomAgent).order_by(CustomAgent.updated_at.desc()).all()
            )
            return CustomAgentSchema.dump_many(rows)

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            row = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            return CustomAgentSchema.dump(row) if row else None

    def get_effective_prompt(self, agent_row: Dict[str, Any]) -> str:
        """Return the system prompt that will actually be sent to Claude."""
        return prompt_for_row(agent_row)

    def create_agent(
        self,
        data: Dict[str, Any],
        created_by: Optional[str] = None,
        changed_by: Optional[str] = None,
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
                model=blank_model(data.get("model")),
                fallback_model=blank_model(data.get("fallback_model")),
                forked_from=data.get("forked_from"),
                created_by=created_by,
            )
            if data.get("component_category"):
                kwargs["component_category"] = data["component_category"]
            agent = CustomAgent(**kwargs)
            # Until the console writes manifests, its payload is rendered into one.
            agent.manifest = manifest_from_row(kwargs)
            # The agent's principal, in the same transaction. A re-created id
            # gets its old principal back rather than a second one.
            principals.ensure_agent(session, agent_id, name)
            session.add(agent)
            _audit_manifest(
                session, agent_id, "create", None, agent.manifest, changed_by
            )
            _audit_model_strings(
                session,
                agent_id,
                "create",
                None,
                model_pair(agent.model, agent.fallback_model),
                changed_by,
            )
            session.flush()
            return CustomAgentSchema.dump(agent)

    def fork_from_profile(
        self,
        source_profile: Any,
        source_id: str,
        created_by: Optional[str] = None,
        new_name: Optional[str] = None,
        changed_by: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a custom agent by copying values from an ``AgentProfile``.

        Works for both built-in and custom source agents. The new row is
        an independent copy — the source is never touched. Appends "
        (copy)" to the name unless ``new_name`` is supplied, and bumps
        with a numeric suffix if the default ID collides. ``overrides`` (the
        ``UPDATABLE_FIELDS``) replace the copied values in the same insert,
        so one call writes one row, or none if validation fails.
        """
        overrides = {
            k: v
            for k, v in (overrides or {}).items()
            if k in UPDATABLE_FIELDS and not (k == "role" and v is None)
        }
        new_name = overrides.pop("name", None) or new_name
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
            "fallback_model": getattr(source_profile, "fallback_model", None),
            "component_category": getattr(source_profile, "component_category", None),
            "forked_from": source_id,
        }
        data.update(overrides)  # an edited prompt wins over the copied rendered one
        data["name"] = name
        return self.create_agent(data, created_by=created_by, changed_by=changed_by)

    def update_agent(
        self,
        agent_id: str,
        updates: Dict[str, Any],
        changed_by: Optional[str] = None,
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
            # A manifest PUT over /api/v1 may hold skills, a guard or a structured
            # conclusion; re-rendering it from the columns would drop them.
            if not is_legacy_shaped(agent.manifest):
                raise ManifestNotLegacy(
                    f"{agent_id} is edited through its manifest: "
                    f"PUT /api/v1/agents/{agent_id}"
                )

            before = model_pair(agent.model, agent.fallback_model)
            manifest_before = agent.manifest
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
                elif key in {"model", "fallback_model"}:
                    setattr(agent, key, blank_model(value))
                else:
                    setattr(agent, key, value)
            agent.manifest = manifest_from_row(CustomAgentSchema.dump(agent))
            _audit_manifest(
                session, agent_id, "update", manifest_before, agent.manifest, changed_by
            )

            _audit_model_strings(
                session,
                agent_id,
                "update",
                before,
                model_pair(agent.model, agent.fallback_model),
                changed_by,
            )
            session.flush()
            return CustomAgentSchema.dump(agent)

    def delete_agent(self, agent_id: str, changed_by: Optional[str] = None) -> bool:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = (
                session.query(CustomAgent)
                .filter(CustomAgent.id == agent_id)
                .one_or_none()
            )
            if not agent:
                return False
            _audit_model_strings(
                session,
                agent_id,
                "delete",
                model_pair(agent.model, agent.fallback_model),
                None,
                changed_by,
            )
            _audit_manifest(
                session, agent_id, "delete", agent.manifest, None, changed_by
            )
            # The principal outlives the agent, so old records still name it.
            principals.mark_deleted(session, principals.agent_principal_id(agent_id))
            session.delete(agent)
            return True

    def list_manifests(self) -> List[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            rows = session.query(CustomAgent).order_by(CustomAgent.id).all()
            return [_manifest_row(row) for row in rows]

    def get_manifest(self, agent_id: str) -> Optional[Dict[str, Any]]:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            row = session.get(CustomAgent, agent_id)
            return _manifest_row(row) if row else None

    def put_manifest(
        self, agent_id: str, text: str, changed_by: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Store a checked manifest under ``agent_id``; (row, created).

        The persona columns are kept in step with it until #4024 drops them, so
        a reader not yet on the manifest sees the same prompt and tools.
        """
        doc = read_manifest(text)
        name = agent_id.removeprefix(CUSTOM_AGENT_ID_PREFIX)
        mirrored = {
            "description": str(doc.get("description") or ""),
            "system_prompt_override": str(doc.get("system") or ""),
            "recommended_tools": manifest_needs(doc),
        }
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = session.get(CustomAgent, agent_id)
            created = agent is None
            before = None if created else agent.manifest
            if created:
                agent = CustomAgent(id=agent_id, name=name, role="", **mirrored)
                principals.ensure_agent(session, agent_id, name)
                session.add(agent)
            else:
                for key, value in mirrored.items():
                    setattr(agent, key, value)
            agent.manifest = text
            _audit_manifest(
                session,
                agent_id,
                "create" if created else "update",
                before,
                text,
                changed_by,
            )
            session.flush()
            return _manifest_row(agent), created


def _manifest_row(row: CustomAgent) -> Dict[str, Any]:
    text = row.manifest or manifest_from_row(CustomAgentSchema.dump(row))
    doc = read_manifest(text)
    return {
        "id": row.id,
        "description": str(doc.get("description") or ""),
        "needs": manifest_needs(doc),
        "skills": [str(s) for s in doc.get("skills") or [] if isinstance(s, str)],
        "manifest": text,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
