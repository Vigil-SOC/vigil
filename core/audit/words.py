"""What a trail entry was, in one short sentence (#3308).

The only place an action key becomes words: the Audit list and the case
Record tab both read ``sentence``. The actor is not in it; each screen names
the actor its own way. An action this table does not know reads as its key.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core.audit import trail


def _word(value: Any) -> str:
    return str(value or "").replace("_", " ").strip()


def _fields(names: Any) -> str:
    if not isinstance(names, (list, tuple)) or not names:
        return ""
    return ": " + ", ".join(_word(n) for n in names)


def _config(p: Mapping[str, Any]) -> str:
    verb = {"create": "Added", "delete": "Removed"}.get(str(p.get("verb")), "Changed")
    key = p.get("config_key") or p.get("config_type")
    return f"{verb} setting {key}" if key else f"{verb} a setting"


def _approve(p: Mapping[str, Any]) -> str:
    if p.get(trail.OUTCOME) == trail.OUTCOME_DENIED:
        reason = " (four-eyes)" if p.get("reason") == "four_eyes" else ""
        return f"Was refused approving an action{reason}"
    return "Approved an action"


def _run_start(p: Mapping[str, Any]) -> str:
    role = _word(p.get("role"))
    return f"Started a {role} run" if role else "Started a run"


def _tool(verb: str) -> Callable[[Mapping[str, Any]], str]:
    return lambda p: f"{verb} {p['tool']}" if p.get("tool") else f"{verb} a tool"


def _checkpoint(p: Mapping[str, Any]) -> str:
    point = p.get("checkpoint")
    seq = point.get("seq") if isinstance(point, Mapping) else None
    return f"Checkpoint at seq {seq}" if seq is not None else "Checkpoint"


def _upgrade(p: Mapping[str, Any]) -> str:
    to = f" to {p['to']}" if p.get("to") else ""
    outcome = p.get(trail.OUTCOME)
    if outcome == "refused":
        return f"The upgrade{to} was refused"
    if outcome == "failed":
        return f"The upgrade{to} failed"
    if p.get("from") and p.get("to"):
        return f"Upgraded from {p['from']} to {p['to']}"
    return f"Upgraded to {p['to']}" if p.get("to") else "Ran an upgrade"


def _case_history(p: Mapping[str, Any]) -> str:
    """A copied case_audit_logs row (0018_legacy_audit_copy)."""
    entity = _word(p.get("entity_type")) or "case"
    verb, field = str(p.get("verb") or ""), _word(p.get("field_name"))
    if verb == "attach":
        return f"Attached a file to the {entity}"
    done = {"create": "Created", "update": "Changed", "delete": "Deleted"}.get(verb)
    if done is None:
        return f"Recorded {_word(verb) or 'a change'} on the {entity}"
    return f"{done} {field} on the {entity}" if field else f"{done} the {entity}"


def _to(field: str) -> Callable[[Mapping[str, Any]], str]:
    def said(p: Mapping[str, Any]) -> str:
        value = _word(p.get(field)).capitalize()
        return f"Changed {field} to {value}" if value else f"Changed {field}"

    return said


def _assign(p: Mapping[str, Any]) -> str:
    return "Unassigned the case" if p.get("unassigned") else "Assigned the case"


def _findings(verb: str, key: str) -> Callable[[Mapping[str, Any]], str]:
    def said(p: Mapping[str, Any]) -> str:
        ids = p.get(key)
        n = len(ids) if isinstance(ids, list) else 0
        return f"{verb} {n} findings" if n > 1 else f"{verb} a finding"

    return said


def _investigation_log(p: Mapping[str, Any]) -> str:
    event = _word(p.get("event_type")) or "an event"
    inv = p.get("investigation_id")
    return f"Logged {event} on investigation {inv}" if inv else f"Logged {event}"


def _task_update(p: Mapping[str, Any]) -> str:
    status = _word(p.get("status"))
    return f"Updated a task, now {status}" if status else "Updated a task"


def _link(p: Mapping[str, Any]) -> str:
    related, kind = p.get("related_case_id"), _word(p.get("relationship_type"))
    if not related:
        return "Linked the case to another"
    return f"Linked the case to {related}" + (f" ({kind})" if kind else "")


def _merge(p: Mapping[str, Any]) -> str:
    moved = p.get("findings_moved")
    count = f", moving {moved} finding{'' if moved == 1 else 's'}"
    return (
        f"Merged {p.get('source_case_id') or 'a case'} into "
        f"{p.get('case_id') or 'this case'}" + (count if moved is not None else "")
    )


def _sla_assign(p: Mapping[str, Any]) -> str:
    policy = p.get("sla_policy_id")
    return f"Assigned SLA policy {policy}" if policy else "Assigned an SLA policy"


def _role(verb: str) -> Callable[[Mapping[str, Any]], str]:
    return lambda p: (
        f"{verb} role {p['role_id']}" if p.get("role_id") else f"{verb} a role"
    )


_SAID: dict[str, str | Callable[[Mapping[str, Any]], str]] = {
    # Sign-in, account and credential (core/auth/trail_events.py and its callers)
    "auth.sign_in": "Signed in",
    "auth.sign_out": "Signed out",
    "auth.sign_in_failed": lambda p: "Failed to sign in"
    + (f": {_word(p['reason'])}" if p.get("reason") else ""),
    "auth.denied": lambda p: "Was refused"
    + (f" {p['attempted']}" if p.get("attempted") else ""),
    "auth.password_changed": "Changed their password",
    "auth.password_reset": "Reset their password",
    "auth.mfa_enabled": "Turned on two-step sign-in",
    "auth.mfa_disabled": "Turned off two-step sign-in",
    "auth.recovery_codes_regenerated": "Made new recovery codes",
    "credential.minted": "Issued an MCP credential",
    "credential.revoked": "Revoked an MCP credential",
    "credential.expired": "Refused an expired MCP credential",
    "credential.refused": "Refused a credential of an inactive account",
    "user.created": "Created an account",
    "user.updated": lambda p: "Updated an account" + _fields(p.get("changed")),
    "user.deleted": "Deleted an account",
    "user.disabled": "Disabled an account",
    "user.enabled": "Enabled an account",
    "user.role_changed": lambda p: "Changed an account's role"
    + (f" to {p['role_id']}" if p.get("role_id") else ""),
    "role.created": _role("Created"),
    "role.updated": _role("Changed"),
    "role.deleted": _role("Deleted"),
    "roles.revised": "Revised the built-in roles",
    "autonomy.tool_classified": lambda p: f"Classed {p.get('tool') or 'a tool'} as "
    f"{_word(p.get('class_after')) or 'unclassified'}",
    "tools.classified": "Classed outside tools on upgrade",
    "findings.technique_confidence_cleared": lambda p: "Cleared technique confidence"
    + (f" on {p['rows_cleared']} findings" if p.get("rows_cleared") else ""),
    # Config (core/storage/config_service.py)
    "config.change": _config,
    # Cases and approvals (core/cases/case_trail.py, agent_closure.py)
    "case.comment": "Commented on the case",
    "case.comment.update": "Edited a comment",
    "case.comment.delete": "Deleted a comment",
    "case.evidence": "Attached a file to the case",
    "case.escalate": "Escalated the case",
    "case.close": "Closed the case",
    "case.create": "Opened the case",
    "case.status": _to("status"),
    "case.priority": _to("priority"),
    "case.assign": _assign,
    "case.edit": lambda p: "Edited the case" + _fields(p.get("changed")),
    "case.findings.link": _findings("Linked", "linked"),
    "case.findings.unlink": _findings("Unlinked", "unlinked"),
    "case.journal": "Added to the journal",
    "case.resolution_step": "Added a resolution step",
    "case.delete": "Deleted the case",
    "case.purge": lambda p: "Deleted every case"
    + (f" ({p['count']})" if p.get("count") else ""),
    "case.task": "Added a task",
    "case.task.update": _task_update,
    "case.link": _link,
    "case.merge": _merge,
    "case.sla.assign": _sla_assign,
    "case.sla.pause": "Paused the SLA",
    "case.sla.resume": "Resumed the SLA",
    "approval.approve": _approve,
    "approval.reject": "Rejected an action",
    # Old audit stores copied on upgrade (0018_legacy_audit_copy)
    "case.history": _case_history,
    "investigation.log": _investigation_log,
    "audit.legacy_dropped": "Dropped the old audit stores on upgrade",
    # Agents (core/agents/attribution.py)
    "agent.run.start": _run_start,
    "agent.tool.intent": _tool("Asked to call"),
    "agent.tool.outcome": _tool("Called"),
    "agent.conclusion": "Concluded a run",
    "agent.outcome": lambda p: "Ended a run"
    + (f": {_word(p['outcome'])}" if p.get("outcome") else ""),
    "agent.directive": lambda p: "Gave a run a directive"
    + (f" ({_word(p['kind'])})" if p.get("kind") else ""),
    # The trail itself (core/audit/)
    trail.ACTION_CHECKPOINT: _checkpoint,
    trail.ACTION_EXPORT: "Exported the trail",
    trail.ACTION_RESTORE: "Restored from a backup",
    trail.ACTION_RETENTION: lambda p: (
        f"Set retention to {p['days']} days"
        if p.get("days")
        else "Set the retention period"
    ),
    trail.ACTION_ERASURE: "Erased a principal's details",
    "audit.feed.failed": "The audit feed failed",
    "audit.feed.recovered": "The audit feed recovered",
    "upgrade.run": _upgrade,
}


def sentence(payload: Any) -> str:
    """One entry's payload as a sentence; an unknown action is its key."""
    p = payload if isinstance(payload, Mapping) else {}
    action = str(p.get(trail.ACTION) or "")
    said = _SAID.get(action)
    try:
        text = said(p) if callable(said) else said
    except Exception:  # noqa: BLE001 - an odd payload still reads as its key
        text = None
    text = text or action or "—"
    collapsed = p.get("collapsed")
    if isinstance(collapsed, Mapping) and collapsed.get("count"):
        text += f" ({collapsed['count']} times)"
    return text
