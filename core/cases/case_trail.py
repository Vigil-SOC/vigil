"""Case and approval actions on the audit trail (#2761).

One append or one ``CaseAuditLog`` insert, in the caller's transaction,
never both: ``trail.in_use`` decides. No revision is added here. The case
Record tab reads both: these entries by ``case_id``, and ``case_audit_logs``.

The case row's own writes (create, edit, journal, delete, purge) append here
too, from ``core/storage/service.py`` through ``case_trail_hook`` (#3403).
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from core.audit import trail
from core.cases.agent_closure import actor_identity, principal_for_username
from core.storage import case_trail_hook
from core.storage.case_trail_hook import TrailWriteError  # noqa: F401 - its home

COMMENT = "case.comment"
COMMENT_UPDATE = "case.comment.update"
COMMENT_DELETE = "case.comment.delete"
EVIDENCE = "case.evidence"
ESCALATE = "case.escalate"
APPROVE = "approval.approve"
REJECT = "approval.reject"
# The case row and its journal (#3403)
CREATE = "case.create"
STATUS = "case.status"
PRIORITY = "case.priority"
ASSIGN = "case.assign"
EDIT = "case.edit"
LINK = "case.findings.link"
UNLINK = "case.findings.unlink"
JOURNAL = "case.journal"
STEP = "case.resolution_step"
DELETE = "case.delete"
PURGE = "case.purge"

# What a write of one field alone is. More than one field is an EDIT.
_ALONE = {
    "status": STATUS,
    "priority": PRIORITY,
    "assignee": ASSIGN,
    "activities": JOURNAL,
    "resolution_steps": STEP,
}
# Free text: beside the hash, never in it.
_TEXT = ("title", "description")


def _payload(action: str, principal: Optional[str], **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {trail.ACTION: action}
    if principal is not None:
        payload[trail.ACTOR] = principal
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


def _beside(actor_text: Optional[str], operator_text: Optional[str]) -> dict:
    beside = {"actor_text": actor_text, "operator_text": operator_text}
    return {k: v for k, v in beside.items() if v is not None}


def _actor(session, signed_in: Optional[str]) -> tuple:
    """``(principal, actor text, on behalf of)``.

    A tool call with signed claims is the agent's, on behalf of the person;
    otherwise ``actor_identity`` names the session or the bound caller.
    """
    from core.auth.tool_principal import current_claims

    claims = current_claims()
    if claims is not None:
        from core.agents.attribution import phase_actor

        agent = phase_actor(claims.run_id or "") or claims.agent_id
        if agent:
            return agent, agent, claims.person_id
    principal, actor_text = actor_identity(session, signed_in)
    return principal, actor_text, None


def append_or_legacy(
    session,
    *,
    action: str,
    signed_in: Optional[str],
    case_id: Optional[str],
    legacy: Any = None,
    operator_text: Optional[str] = None,
    ids: Optional[Callable[[], dict[str, Any]]] = None,
) -> None:
    """Append, or insert ``legacy`` when the trail is not in use.

    ``signed_in`` is the username the caller took from the session or from
    ``current_caller()``, never a request body; ``actor_identity`` resolves
    its principal, and an unset caller is ``agent`` with none. ``ids`` runs
    after a flush, so an id the insert assigns is in the hashed payload.
    Previous comment text and an escalation reason go in ``operator_text``.
    """
    # Any failure on the trail path is a TrailWriteError, so a joined caller
    # sees it whatever the cause, not only from the append itself.
    try:
        use_trail = trail.in_use(session)
        if use_trail:
            session.flush()
            principal, actor_text, on_behalf_of = _actor(session, signed_in)
            fields = {
                "case_id": case_id,
                **(ids() if ids else {}),
                trail.ON_BEHALF_OF: on_behalf_of,
            }
            trail.append(
                session,
                _payload(action, principal, **fields),
                **_beside(actor_text, operator_text),
            )
    except trail.Paused:
        raise  # a restore's pause answers 503 with its retry time, not 500
    except Exception as exc:
        raise TrailWriteError(str(exc)) from exc
    if not use_trail and legacy is not None:
        session.add(legacy)


def append_case(
    session,
    action: str,
    case_id: Optional[str],
    *,
    operator_text: Optional[str] = None,
    **fields: Any,
) -> None:
    """One case-row entry in the caller's transaction; no legacy row (#3403).

    The caller is whoever is bound (``actor_identity``), never an argument.
    A callable field is read only when the trail is in use.
    """
    append_or_legacy(
        session,
        action=action,
        signed_in=None,
        case_id=case_id,
        operator_text=operator_text,
        ids=lambda: {k: v() if callable(v) else v for k, v in fields.items()},
    )


def assignee_principal(session, username: Optional[str]) -> Callable[[], Any]:
    """An assignee's principal id, looked up when the entry is written."""
    return lambda: principal_for_username(session, username) if username else None


def append_update(
    session,
    case_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    findings: tuple[Sequence[str], Sequence[str]] = ((), ()),
) -> None:
    """One entry for one ``update_case``, naming the fields that changed.

    ``before`` and ``after`` are the written columns' values; ``findings`` is
    the ids linked and unlinked. A write that changes nothing appends nothing.
    """
    changed = [k for k in after if before.get(k) != after[k]]
    # A close is CaseWorkflowService.close_case's entry; not a second one here.
    if after.get("status") == "closed" and "status" in changed:
        changed.remove("status")
    linked, unlinked = list(findings[0]), list(findings[1])
    if linked or unlinked:
        changed.append("findings")
    if not changed:
        return

    fields: dict[str, Any] = {"changed": sorted(changed)}
    texts: list[str] = []
    for key in ("status", "priority"):
        if key in changed:
            fields[key] = after[key]
    if "assignee" in changed:
        fields["assignee"] = assignee_principal(session, after["assignee"])
        if after["assignee"]:
            texts.append(str(after["assignee"]))
        else:
            fields["unassigned"] = True
    if changed == ["findings"]:
        action = LINK if not unlinked else UNLINK if not linked else EDIT
    else:
        action = _ALONE.get(changed[0], EDIT) if len(changed) == 1 else EDIT
    if linked:
        fields["linked"] = linked
    if unlinked:
        fields["unlinked"] = unlinked

    # A journal line or step appended alone is one new item: its index lets
    # the Record tab show it once, and its text sits beside the hash.
    if action in (JOURNAL, STEP):
        key = changed[0]
        old, new = before.get(key) or [], after[key] or []
        if len(new) == len(old) + 1 and list(new[: len(old)]) == list(old):
            item = new[-1] if isinstance(new[-1], Mapping) else {}
            if action == JOURNAL:
                fields["activity"] = len(old)
            texts.append(str(item.get("description") or ""))
        else:
            action = EDIT
    for key in _TEXT:
        if key in changed and after[key]:
            texts.append(str(after[key]))
    append_case(
        session,
        action,
        case_id,
        operator_text="\n".join(t for t in texts if t) or None,
        **fields,
    )


def append_approval(
    session,
    *,
    action: str,
    principal_id: Optional[str],
    action_id: str,
    username: Optional[str],
    operator_text: Optional[str] = None,
) -> None:
    """Append an approve or reject in the caller's transaction.

    ``principal_id`` is the one the caller already resolved; ``approved_by``
    and ``approved_by_principal_id`` are not changed. No legacy row:
    approvals had none.
    """
    if not trail.in_use(session):
        return
    trail.append(
        session,
        _payload(action, principal_id, action_id=action_id),
        **_beside(username or None, operator_text),
    )


# The case row's writes (core/storage/service.py) append through this module.
case_trail_hook.install(sys.modules[__name__])
