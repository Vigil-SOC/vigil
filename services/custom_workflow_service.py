"""
CRUD service for database-backed custom workflows.

Provides persistence for user-created workflows alongside the file-based
WORKFLOW.md definitions that WorkflowsService already loads from disk.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from database.connection import get_db_manager
from database.models import CustomWorkflow

logger = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Produce a short, URL-safe slug from a workflow name."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug[:60] or "workflow"


def _generate_workflow_id(name: str) -> str:
    """Build a unique workflow_id: wf-<slug>-<short-uuid>."""
    return f"wf-{_slugify(name)}-{uuid.uuid4().hex[:8]}"


def _validate_agent_ids(phases: List[Dict[str, Any]]) -> None:
    """Ensure every phase's agent_id resolves against the unified pool.

    Avoids the silent-failure-at-execution-time pattern where a workflow
    references a renamed/deleted custom agent. Built-ins + DB-backed
    custom agents are both checked. Raises ``ValueError`` listing every
    unknown id so the UI can surface all of them at once.
    """
    if not phases:
        return
    # Deferred to keep this service import-cheap for callers that only
    # want a .get() and don't touch the AgentManager.
    try:
        from services.soc_agents import AgentManager

        known = set(AgentManager().agents.keys())
    except Exception as e:
        # If the agent registry isn't available (e.g. DB down during a
        # migration) we skip validation rather than block writes.
        logger.warning("Skipping workflow agent validation: %s", e)
        return

    missing: List[str] = []
    for idx, phase in enumerate(phases, start=1):
        aid = (phase or {}).get("agent_id")
        if aid and aid not in known:
            missing.append(f"phase {idx}: '{aid}'")
    if missing:
        raise ValueError(
            "Unknown agent_id(s) in workflow phases: "
            + "; ".join(missing)
            + ". Fork one of the built-in templates or pick an existing custom agent."
        )


class CustomWorkflowService:
    """Persistence service for database-backed custom workflows."""

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a new custom workflow.

        Args:
            payload: Dict with keys matching CustomWorkflow columns. Required:
                name, description, phases. Optional: use_case, trigger_examples,
                graph_layout, created_by, workflow_id.

        Returns:
            The created workflow as a dict.
        """
        if not payload.get("name"):
            raise ValueError("name is required")
        if not payload.get("description"):
            raise ValueError("description is required")

        _validate_agent_ids(payload.get("phases") or [])

        workflow_id = payload.get("workflow_id") or _generate_workflow_id(
            payload["name"]
        )

        db = get_db_manager()
        with db.session_scope() as session:
            wf = CustomWorkflow(
                workflow_id=workflow_id,
                name=payload["name"],
                description=payload["description"],
                use_case=payload.get("use_case"),
                trigger_examples=payload.get("trigger_examples") or [],
                phases=payload.get("phases") or [],
                graph_layout=payload.get("graph_layout") or {},
                is_active=payload.get("is_active", True),
                created_by=payload.get("created_by"),
                version=1,
            )
            session.add(wf)
            session.flush()
            result = wf.to_dict()
        logger.info(f"Created custom workflow: {workflow_id}")
        return result

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Get a custom workflow by ID."""
        db = get_db_manager()
        with db.session_scope() as session:
            wf = session.get(CustomWorkflow, workflow_id)
            return wf.to_dict() if wf else None

    def list(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """List custom workflows."""
        db = get_db_manager()
        with db.session_scope() as session:
            stmt = select(CustomWorkflow)
            if active_only:
                stmt = stmt.where(CustomWorkflow.is_active.is_(True))
            stmt = stmt.order_by(CustomWorkflow.updated_at.desc())
            rows = session.execute(stmt).scalars().all()
            return [r.to_dict() for r in rows]

    def update(
        self, workflow_id: str, updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Update a workflow. Increments version on every update.

        Args:
            workflow_id: The workflow to update.
            updates: Partial dict of fields to change.

        Returns:
            Updated dict, or None if not found.
        """
        allowed = {
            "name",
            "description",
            "use_case",
            "trigger_examples",
            "phases",
            "graph_layout",
            "is_active",
        }
        if "phases" in updates and updates["phases"] is not None:
            _validate_agent_ids(updates["phases"])

        db = get_db_manager()
        with db.session_scope() as session:
            wf = session.get(CustomWorkflow, workflow_id)
            if not wf:
                return None

            for key, value in updates.items():
                if key in allowed and value is not None:
                    setattr(wf, key, value)
            wf.version = (wf.version or 1) + 1
            wf.updated_at = datetime.utcnow()
            session.flush()
            result = wf.to_dict()
        logger.info(
            f"Updated custom workflow: {workflow_id} (version={result['version']})"
        )
        return result

    def delete(self, workflow_id: str) -> bool:
        """Soft-delete by setting is_active=False. Returns True if found."""
        db = get_db_manager()
        with db.session_scope() as session:
            wf = session.get(CustomWorkflow, workflow_id)
            if not wf:
                return False
            wf.is_active = False
            wf.updated_at = datetime.utcnow()
        logger.info(f"Soft-deleted custom workflow: {workflow_id}")
        return True


_service: Optional[CustomWorkflowService] = None


def get_custom_workflow_service() -> CustomWorkflowService:
    """Get the singleton CustomWorkflowService instance."""
    global _service
    if _service is None:
        _service = CustomWorkflowService()
    return _service
