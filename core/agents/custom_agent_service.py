"""Operator agents: plugin manifests stored on custom_agents (#4023, #4024)."""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from core.agents.manager import CUSTOM_AGENT_ID_PREFIX
from core.agents.operator_manifest import manifest_needs, read_manifest
from core.auth import principals
from core.storage.config_service import write_config_audit
from core.storage.connection import get_db_manager
from core.storage.models import CustomAgent

logger = logging.getLogger(__name__)

_ID_LINE = re.compile(r"^id:.*$", re.MULTILINE)


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
    ) -> Tuple[Dict[str, Any], bool]:
        """Store a manifest under ``agent_id``; (row, created)."""
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            agent = session.get(CustomAgent, agent_id)
            created = agent is None
            before = None if created else agent.manifest
            if created:
                agent = CustomAgent(id=agent_id, manifest=text)
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
        self, source: str, manifest: str, changed_by: Optional[str] = None
    ) -> Dict[str, Any]:
        """Store a built-in's manifest text under the first free
        ``custom-<id>[-n]``, only its id line rewritten (as the drawer's Copy
        does), so placeholders and comments survive."""
        base = f"{CUSTOM_AGENT_ID_PREFIX}{source}"
        db_manager = get_db_manager()
        for n in range(1, 1000):
            new_id = base if n == 1 else f"{base}-{n}"
            text = _ID_LINE.sub(f"id: {new_id}", manifest, count=1)
            try:
                with db_manager.session_scope() as session:
                    # Insert only: an id taken meanwhile is skipped, never replaced.
                    if session.get(CustomAgent, new_id) is not None:
                        continue
                    session.add(
                        CustomAgent(id=new_id, manifest=text, forked_from=source)
                    )
                    principals.ensure_agent(
                        session, new_id, new_id.removeprefix(CUSTOM_AGENT_ID_PREFIX)
                    )
                    _audit_manifest(session, new_id, "create", None, text, changed_by)
                    session.flush()
                    return _manifest_row(session.get(CustomAgent, new_id))
            except IntegrityError:
                continue
        raise RuntimeError(f"no free id for a fork of {source}")

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
