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


def _sign_in_ended(p: Mapping[str, Any]) -> str:
    if p.get("end_reason") == "too_many_confirmations":
        return "Had a sign-in ended by three failed confirmations"
    # Whose it was is the entry's principal; the actor is named by the screen.
    own = p.get(trail.ACTOR) == p.get("principal")
    return "Ended one of their own sign-ins" if own else "Ended someone's sign-in"


_KEY_PURPOSES = {
    "signin": "sign-in",
    "run": "run-token",
    "reset": "reset-link",
    "setup": "first-run code",
    "mfa": "authenticator-secret",
    "webauthn": "security-key-challenge",
    "sso": "single sign-on state",
}


def _key_purpose(p: Mapping[str, Any]) -> str:
    return _KEY_PURPOSES.get(str(p.get("purpose")), "signing")


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
    said = f"Updated a task, now {status}" if status else "Updated a task"
    if p.get("unassigned"):
        return f"{said}, unassigned"
    if p.get("assignee") or p.get("assignee_unresolved"):
        moved = p.get("previous_assignee") or p.get("previous_assignee_unresolved")
        return f"{said}, {'reassigned' if moved else 'assigned'}"
    return said


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


def _noise_mark(p: Mapping[str, Any]) -> str:
    return f"Marked {p.get('finding_id') or 'an alert'} as noise"


def _noise_clear(p: Mapping[str, Any]) -> str:
    return f"Cleared the noise mark on {p.get('finding_id') or 'an alert'}"


def _import(verb: str) -> Callable[[Mapping[str, Any]], str]:
    return lambda p: (
        f"{verb} history {p['import_id']}" if p.get("import_id") else f"{verb} history"
    )


def _note(verb: str) -> Callable[[Mapping[str, Any]], str]:
    return lambda p: (
        f"{verb} a note on {p['entity_key']}"
        if p.get("entity_key")
        else f"{verb} a note"
    )


def _role(verb: str) -> Callable[[Mapping[str, Any]], str]:
    return lambda p: (
        f"{verb} role {p['role_id']}" if p.get("role_id") else f"{verb} a role"
    )


def _proof_export(p: Mapping[str, Any]) -> str:
    n = p.get("rule_count")
    if not p.get("kind"):
        return "Exported a proof card"
    return f"Exported a proof card ({p['kind']}, {n} {'rule' if n == 1 else 'rules'})"


def _skill(verb: str):
    def words(p: Mapping[str, Any]) -> str:
        name = f" {p['name']!r}" if p.get("name") else ""
        where = f" from {p['exported_from']}" if p.get("exported_from") else ""
        return f"{verb} the Detection Skill{name}{where}"

    return words


# Who needed the key is the entry's on_behalf_of, which the trail names when it is
# read; the sentence says what needed it and never prints an id.
def _proof_key(p: Mapping[str, Any]) -> str:
    made = {
        "export": " on a first export",
        "key_read": " when its public key was first read",
    }
    return "Made the proof card signing key" + made.get(p.get("made_by"), "")


def _staged(p: Mapping[str, Any]) -> str:
    rule = f"rule {p['rule_id']}" if p.get("rule_id") is not None else "a rule"
    version = f" v{p['version']}" if p.get("version") is not None else ""
    to = _word(p.get("to_stage")) or "a new stage"
    # A person's Revert sets a hold, a release lifts it; Vigil's moves leave it.
    held = p.get("hold")
    hold = " and held it there" if held is True else ""
    hold = " and released its hold" if held is False else hold
    if p.get("hold") is False and p.get("from_stage") == p.get("to_stage"):
        return f"Released the hold on {rule}{version} in {to}"
    if p.get("from_stage"):
        return f"Moved {rule}{version} from {_word(p['from_stage'])} to {to}{hold}"
    return f"Put {rule}{version} in {to}{hold}"


def _saved(p: Mapping[str, Any]) -> str:
    rule = f"rule {p['rule_id']}" if p.get("rule_id") is not None else "a rule"
    version = f" v{p['version']}" if p.get("version") is not None else ""
    language = f" ({_word(p['language'])} copy)" if p.get("language") else ""
    return f"Saved {rule}{version}{language}"


def _ingested(p: Mapping[str, Any]) -> str:
    collapsed = p.get("collapsed")
    total = collapsed.get("count") if isinstance(collapsed, Mapping) else None
    n = total if total is not None else p.get("count")
    found = f"{n} finding{'' if n == 1 else 's'}" if isinstance(n, int) else "findings"
    source = f" from {p['data_source']}" if p.get("data_source") else ""
    return f"Ingested {found}{source}" + (" within a minute" if total else "")


# Actions whose sentence already says the window's count.
_COUNTED = frozenset({"findings.ingested"})

_SAID: dict[str, str | Callable[[Mapping[str, Any]], str]] = {
    # Sign-in, account and credential (core/auth/trail_events.py and its callers)
    "auth.sign_in": lambda p: "Signed in"
    + (" through single sign-on" if p.get("method") == "sso" else ""),
    "auth.sign_out": "Signed out",
    "auth.sign_in_ended": _sign_in_ended,
    "auth.confirmed": lambda p: "Confirmed it's them"
    + (" with a password and a second step" if p.get("strong") else ""),
    "auth.confirmation_failed": "Failed to confirm it's them",
    "auth.everyone_signed_out": "Signed everyone out",
    "auth.sign_in_failed": lambda p: "Failed to sign in"
    + (f": {_word(p['reason'])}" if p.get("reason") else ""),
    "auth.denied": lambda p: "Was refused"
    + (f" {p['attempted']}" if p.get("attempted") else ""),
    "auth.password_changed": "Changed their password",
    "auth.password_reset": "Reset their password",
    "auth.mfa_enabled": "Turned on two-step sign-in",
    "auth.mfa_disabled": "Turned off two-step sign-in",
    "auth.recovery_codes_regenerated": "Made new recovery codes",
    "auth.authenticator_app_removed": "Removed their authenticator app",
    "auth.security_key_added": "Registered a security key",
    "auth.security_key_removed": "Removed a security key",
    "auth.mfa_recovered": "Signed in with a recovery code, which removed their methods",
    "auth.keys_adopted": "Adopted the signing and encryption key ring",
    "auth.key_rotated": lambda p: f"Rotated the {_key_purpose(p)} key",
    "auth.key_retired": lambda p: f"Retired the {_key_purpose(p)} key",
    "auth.key_ring_reset": "Replaced the key ring: a restore could not open it",
    "auth.key_ring_rewrapped": "Wrapped the key ring under this install's secret",
    "auth.wrapping_key_rotated": "Rotated the secret that wraps the key ring",
    "auth.wrapping_key_due": "The secret that wraps the key ring is due for rotation",
    "auth.wrapping_key_overdue": "The secret that wraps the key ring is overdue",
    "auth.key_rotation_failed": lambda p: f"Could not rotate the {_key_purpose(p)} key",
    "auth.key_rotation_recovered": lambda p: f"Rotated the late {_key_purpose(p)} key",
    "auth.mfa_reset_all": "Cleared every authenticator app",
    "auth.emergency_rotation": "Rotated keys with no overlap, in an emergency",
    "credential.minted": "Issued an API token",
    "credential.rotated": lambda p: "Rotated an API token"
    + (" (suspected compromise)" if p.get("compromised") else ""),
    "credential.revoked": "Revoked an API token",
    "credential.expired": "Refused an expired API token",
    "credential.refused": "Refused a token of an inactive account",
    "credential.expiry_set": "Set an API token to expire in 90 days",
    "user.created": lambda p: "Created an account"
    + (" through single sign-on" if p.get("via") == "sso" else ""),
    "user.updated": lambda p: "Updated an account" + _fields(p.get("changed")),
    "user.deleted": "Deleted an account",
    "user.disabled": "Disabled an account",
    "user.enabled": "Enabled an account",
    "user.restored": "Restored a deleted account",
    "user.role_changed": lambda p: "Changed an account's role"
    + (f" to {p['role_id']}" if p.get("role_id") else ""),
    "role.created": _role("Created"),
    "role.updated": _role("Changed"),
    "role.deleted": _role("Deleted"),
    "roles.revised": "Revised the built-in roles",
    "detections.proof_card_exported": _proof_export,
    "detections.proof_key_created": _proof_key,
    "detections.skill_exported": _skill("Exported"),
    "detections.skill_imported": _skill("Imported"),
    "detections.skill_import_refused": lambda p: "Refused a Detection Skill import"
    + (f": {p['reason']}" if p.get("reason") else ""),
    "autonomy.tool_classified": lambda p: f"Classed {p.get('tool') or 'a tool'} as "
    f"{_word(p.get('class_after')) or 'unclassified'}",
    "tools.classified": "Classed outside tools on upgrade",
    "detections.rule_staged": _staged,
    "detections.rule_saved": _saved,
    "findings.ingested": _ingested,
    "findings.technique_confidence_cleared": lambda p: "Cleared technique confidence"
    + (f" on {p['rows_cleared']} findings" if p.get("rows_cleared") else ""),
    # Config (core/storage/config_service.py)
    "config.change": _config,
    # Cases and approvals (core/cases/case_trail.py, agent_closure.py)
    "case.comment": "Commented on the case",
    "case.comment.update": "Edited a comment",
    "case.comment.delete": "Deleted a comment",
    "case.evidence": lambda p: (
        "Attached a file to the case"
        if p.get("file_hash_sha256")
        else "Added evidence to the case"
    ),
    "case.evidence.custody": "Added to an evidence item's chain of custody",
    "case.ioc": lambda p: "Added an indicator to the case"
    + (f" ({_word(p['ioc_type'])})" if p.get("ioc_type") else ""),
    "case.ioc.bulk": lambda p: (
        f"Added {p['count']} indicator{'' if p['count'] == 1 else 's'} to the case"
        if isinstance(p.get("count"), int)
        else "Added indicators to the case"
    ),
    "case.watcher.add": "Added a watcher to the case",
    "case.watcher.remove": "Removed a watcher from the case",
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
    "approval.request": lambda p: "Asked for approval"
    + (f" of {_word(p['action_type'])}" if p.get("action_type") else ""),
    trail.ACTION_FINDING_NOISE_MARK: _noise_mark,
    trail.ACTION_FINDING_NOISE_CLEAR: _noise_clear,
    # Analyst notes on an entity (core/memory/notes.py)
    "memory.note.written": _note("Wrote"),
    "memory.note.withdrawn": _note("Withdrew"),
    # Imported history (core/memory/imported_history.py)
    "memory.import.written": _import("Imported"),
    "memory.import.deleted": _import("Deleted"),
    # Old audit stores copied on upgrade (0018_legacy_audit_copy)
    "case.history": _case_history,
    "investigation.log": _investigation_log,
    "audit.legacy_dropped": "Dropped the old audit stores on upgrade",
    # Agents (core/agents/attribution.py)
    "agent.run.start": _run_start,
    "agent.tool.intent": _tool("Asked to call"),
    "agent.tool.outcome": _tool("Called"),
    "agent.run.resume": "Resumed a run after a decision",
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
    "audit.feed.failed": lambda p: (
        "The audit webhook failed"
        if p.get("destination") == "webhook"
        else "The audit feed failed"
    ),
    "audit.feed.recovered": lambda p: (
        "The audit webhook recovered"
        if p.get("destination") == "webhook"
        else "The audit feed recovered"
    ),
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
    if (
        isinstance(collapsed, Mapping)
        and collapsed.get("count")
        and action not in _COUNTED
    ):
        text += f" ({collapsed['count']} times)"
    return text
