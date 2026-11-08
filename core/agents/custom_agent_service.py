"""Operator agents: plugin manifests stored on custom_agents (#4023, #4024)."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.agents.manager import CUSTOM_AGENT_ID_PREFIX
from core.agents.operator_manifest import (
    manifest_from_row,
    manifest_needs,
    read_manifest,
)
from core.auth import principals
from core.storage.config_service import write_config_audit
from core.storage.connection import get_db_manager
from core.storage.models import CustomAgent

logger = logging.getLogger(__name__)


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


class CustomAgentService:
    """Read, store, fork and delete operator manifests."""

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
        self,
        agent_id: str,
        text: str,
        changed_by: Optional[str] = None,
        forked_from: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Store a manifest under ``agent_id``; (row, created)."""
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = session.get(CustomAgent, agent_id)
            created = agent is None
            before = None if created else agent.manifest
            if created:
                agent = CustomAgent(id=agent_id, manifest=text, forked_from=forked_from)
                principals.ensure_agent(
                    session, agent_id, agent_id.removeprefix(CUSTOM_AGENT_ID_PREFIX)
                )
                session.add(agent)
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

    def fork_builtin(
        self, row: Dict[str, Any], changed_by: Optional[str] = None
    ) -> Dict[str, Any]:
        """Store a built-in's record as a manifest under the first free
        ``custom-<id>[-n]``, converted the way the migration converts a row.
        ``row`` is the built-in's record with its confidence bands rendered."""
        source = row["id"]
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            base = f"{CUSTOM_AGENT_ID_PREFIX}{source}"
            new_id, n = base, 2
            while session.get(CustomAgent, new_id) is not None:
                new_id, n = f"{base}-{n}", n + 1
        text = manifest_from_row({**row, "id": new_id})
        saved, _ = self.put_manifest(
            new_id, text, changed_by=changed_by, forked_from=source
        )
        return saved

    def delete_agent(self, agent_id: str, changed_by: Optional[str] = None) -> bool:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = session.get(CustomAgent, agent_id)
            if not agent:
                return False
            _audit_manifest(
                session, agent_id, "delete", agent.manifest, None, changed_by
            )
            # The principal outlives the agent, so old records still name it.
            principals.mark_deleted(session, principals.agent_principal_id(agent_id))
            session.delete(agent)
            return True


def _manifest_row(row: CustomAgent) -> Dict[str, Any]:
    doc = read_manifest(row.manifest)
    return {
        "id": row.id,
        "description": str(doc.get("description") or ""),
        "needs": manifest_needs(doc),
        "skills": [str(s) for s in doc.get("skills") or [] if isinstance(s, str)],
        "manifest": row.manifest,
        "forked_from": row.forked_from,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
