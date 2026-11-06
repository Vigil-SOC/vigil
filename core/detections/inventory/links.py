"""An agent's or person's decision on which repo rule a SIEM copy came from.

The decision is the copy's link columns: 'agent' (linked), 'declined' (this
copy is not from that repo rule) or none (unlinked, back to the sync's
same_id / known_name matchers). The sync never touches an 'agent' or 'declined'
row. Each call is written to the config audit log in the same transaction as the change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.cases.agent_closure import actor, run_agent
from core.detections.inventory.catalog import in_repo
from core.storage.config_service import ConfigService
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule

ACTIONS = ("link", "unlink", "decline")
AUDIT_TYPE = "detection_rule_link"


def _int(value: Any) -> Optional[int]:
    """An int, or a string of digits; never a bool or a float."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _value(rule: DetectionRule) -> Dict[str, Any]:
    return {
        "origin_rule_id": rule.origin_rule_id,
        "link_kind": rule.link_kind,
        "linked_by": rule.linked_by,
        "link_reason": rule.link_reason,
        "linked_at": rule.linked_at.isoformat() if rule.linked_at else None,
    }


def _who() -> tuple:
    """(who decided, the run's agent and run id): never an argument the model gave.

    A run no person started has no caller, only its signed claims.
    """
    agent_id, run_id = run_agent()
    run = {"agent_id": agent_id, "run_id": run_id}
    return actor(), {k: v for k, v in run.items() if v}


def link_rule(
    copy_rule_id: Any, action: str, reason: str, origin_rule_id: Any = None
) -> Dict[str, Any]:
    action = str(action or "").strip().lower()
    reason = str(reason or "").strip()
    copy_id, origin_id = _int(copy_rule_id), _int(origin_rule_id)
    if action not in ACTIONS:
        return {"error": f"action must be one of {', '.join(ACTIONS)}"}
    if not reason:
        return {"error": "A reason is required"}
    if copy_id is None:
        return {"error": "copy_rule_id must be a rule_id"}
    if action != "unlink" and origin_id is None:
        return {"error": f"origin_rule_id is required to {action}"}
    if origin_rule_id is not None and origin_id is None:
        return {"error": "origin_rule_id must be a rule_id"}

    who, run = _who()
    with get_db_manager().session_scope() as session:
        # Locked so a sync linking this copy now waits, then sees the decision.
        copy = session.get(DetectionRule, copy_id, with_for_update=True)
        if copy is None:
            return {"error": f"No detection rule {copy_id}"}
        if in_repo(copy.source):
            return {"error": f"Rule {copy_id} is a repo rule, not a SIEM copy"}
        if origin_id is not None:
            origin = session.get(DetectionRule, origin_id)
            if origin is None:
                return {"error": f"No detection rule {origin_id}"}
            if not in_repo(origin.source):
                return {"error": f"Rule {origin_id} is not a repo rule"}
            if action == "link" and origin.status == "removed":
                return {"error": f"Repo rule {origin_id} is removed"}
        if action == "unlink":
            if copy.link_kind is None:
                return {"error": f"Rule {copy_id} has no link or decline to undo"}
            if origin_id is not None and origin_id != copy.origin_rule_id:
                return {
                    "error": f"Rule {copy_id} is not linked to {origin_id}",
                    "current": _value(copy),
                }

        old = _value(copy)
        if action == "unlink":
            copy.origin_rule_id = copy.link_kind = copy.linked_by = None
            copy.link_reason = copy.linked_at = None
        else:
            copy.origin_rule_id = origin_id
            copy.link_kind = "agent" if action == "link" else "declined"
            copy.linked_by, copy.link_reason, copy.linked_at = (
                who,
                reason,
                datetime.now(timezone.utc),
            )
        new = _value(copy)
        ConfigService(user_id=who)._create_audit_log(
            session,
            config_type=AUDIT_TYPE,
            config_key=f"detection_rule:{copy_id}",
            action=action,
            old_value=old,
            new_value={**new, **run},
            change_reason=reason,
        )
    return {
        "success": True,
        "rule_id": copy_id,
        "action": action,
        "old": old,
        "new": new,
    }
