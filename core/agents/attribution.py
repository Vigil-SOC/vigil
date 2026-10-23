"""Who an agent run acts for, and the trail entries that record it.

The agent service carries the signed token and does not write the trail.
Callers append through :func:`core.audit.trail.append` once the trail table
is there. A database the upgrade has not reached keeps today's writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.audit import trail
from core.auth.principals import SYSTEM_PRINCIPAL_ID, agent_principal_id
from core.auth.tool_principal import Claims
from core.llm.chat_layers import _READONLY_LEADS
from core.response.approval_service import APPROVAL_CONFIG_KEY
from core.response.config import ResponseConfig
from core.storage.config_service import get_config_service

logger = logging.getLogger(__name__)

_KIND_PLUGIN = {
    "hunt": "hunt",
    "root_cause": "rootcause",
    "adjudicate": "adjudicate",
    "investigate": "investigate",
    "compose": "compose",
    "chat": "chat",
    "tally": "tally",
}
_TARGET_KEYS = (
    "target",
    "host",
    "hostname",
    "ip",
    "case_id",
    "finding_id",
    "id",
    "name",
)


class TrailWriteError(Exception):
    """The trail is up and refused the entry. The action must not proceed."""


_current: ContextVar[Optional[Claims]] = ContextVar("vigil_tool_claims", default=None)


@contextmanager
def bound_claims(claims: Optional[Claims]) -> Iterator[None]:
    """The signed principal for the tool call running on this task."""
    if claims is None:
        yield
        return
    token = _current.set(claims)
    try:
        yield
    finally:
        _current.reset(token)


def current_claims() -> Optional[Claims]:
    return _current.get()


def plugin_actor(run_kind: str) -> str:
    """``agent:<plugin>`` for a run no phase agent names."""
    return agent_principal_id(_KIND_PLUGIN.get(run_kind, run_kind))


def phase_actors(phases: Any) -> dict[str, str]:
    """Phase id to the agent principal the playbook names for it."""
    found: dict[str, str] = {}
    for index, phase in enumerate(phases or []):
        if not isinstance(phase, dict):
            continue
        agent = phase.get("agent") or phase.get("agent_id") or ""
        if not agent:
            continue
        phase_id = str(phase.get("id") or phase.get("phase_id") or f"phase-{index + 1}")
        named = str(agent)
        found[phase_id] = (
            named if named.startswith("agent:") else agent_principal_id(named)
        )
    return found


def _stored_force_manual() -> bool:
    """The Settings row, OR'd with the env flag the way the console chip is.

    A database that cannot be read adds nothing: the env flag still stands.
    """
    try:
        row = get_config_service().get_system_config(APPROVAL_CONFIG_KEY)
    except Exception:  # noqa: BLE001 - no row is not a force
        return False
    return bool(row.get("enabled")) if isinstance(row, dict) else False


def autonomy_label() -> str:
    """Assist when approval is forced or auto-response is off, otherwise Act."""
    config = ResponseConfig.from_settings()
    forced = config.force_manual_approval or _stored_force_manual()
    return "Assist" if forced or not config.auto_response_enabled else "Act"


def fingerprint(spec: Any) -> str:
    """sha256 of the canonical JSON of a run spec."""
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def effectful(tool: str) -> bool:
    """A tool is read-only only when its lead token is a known read.

    Every other name, including one this process has never heard of, is an
    effect. ``changes_for_tool`` is the wrong check: it calls Vigil's own
    case writes read-only.
    """
    lead = tool.split("_", 1)[0] if tool else ""
    return lead not in _READONLY_LEADS


def target_of(args: Any) -> Optional[str]:
    """One identifier the call names. Never the argument object."""
    if not isinstance(args, dict):
        return None
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value and len(value) <= 200:
            return value
    return None


def attributed(claims: Optional[Claims]) -> bool:
    return bool(claims and claims.agent_id and claims.run_id)


def trail_writable(session: Session) -> bool:
    """The table is there and the revision that created it has been applied."""
    if (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is None
    ):
        return False
    return trail.trail_ready(session)


def _open():
    from core.storage.connection import get_db_session

    try:
        return get_db_session()
    except Exception:  # noqa: BLE001 - no database is today's path
        logger.warning("audit trail skipped: no database session")
        return None


def append_now(payload: dict[str, Any]) -> bool:
    """Commit one entry. False when the trail is not in use.

    Raises :class:`TrailWriteError` when it is in use and the write fails,
    so the caller can refuse the action.
    """
    session = _open()
    if session is None:
        return False
    try:
        try:
            writable = trail_writable(session)
        except OperationalError:
            logger.warning("audit trail skipped: database unreachable")
            return False
        if not writable:
            return False
        trail.append(session, payload)
        session.commit()
        return True
    except OperationalError:
        session.rollback()
        logger.warning("audit trail skipped: database unreachable")
        return False
    except Exception as exc:
        session.rollback()
        raise TrailWriteError(str(exc)) from exc
    finally:
        session.close()


def append_here(session: Session, payload: dict[str, Any]) -> bool:
    """Append in the caller's transaction. False when the trail is not in use."""
    if not trail_writable(session):
        return False
    trail.append(session, payload)
    return True


def note_run_start(
    *,
    run_id: str,
    actor: str,
    origin_kind: str,
    origin: str,
    spec: Any,
    model: Optional[str] = None,
) -> None:
    payload: dict[str, Any] = {
        "action": "agent.run.start",
        "actor": actor,
        "origin_kind": origin_kind,
        "origin": origin,
        "autonomy": autonomy_label(),
        "fingerprint": fingerprint(spec),
        "run_id": run_id,
    }
    if origin_kind == "person" and origin:
        payload["on_behalf_of"] = origin
    if model:
        payload["model"] = model
    append_now(payload)


def note_child(
    session: Session,
    *,
    parent_run_id: str,
    child_run_id: str,
    spec: Any,
    role: str = "",
) -> str:
    """A child run's actor is the parent's agent; its origin is the parent run.

    ``written`` when this call appended it, ``present`` when it was already
    there, ``unavailable`` when the trail is not in use, ``unlinked`` when the
    parent's start is not on the trail yet.
    """
    if not trail_writable(session):
        return "unavailable"
    if _start_payload(session, child_run_id) is not None:
        return "present"
    parent = _start_payload(session, parent_run_id)
    if parent is None:
        return "unlinked"
    payload: dict[str, Any] = {
        "action": "agent.run.start",
        "actor": parent.get("actor") or SYSTEM_PRINCIPAL_ID,
        "origin_kind": "parent",
        "origin": parent_run_id,
        "autonomy": parent.get("autonomy") or autonomy_label(),
        "fingerprint": fingerprint(spec),
        "run_id": child_run_id,
    }
    if role:
        payload["role"] = role
    trail.append(session, payload)
    return "written"


def principal_id_for_username(username: Optional[str]) -> Optional[str]:
    """The person's principal, when the token named them and not their id."""
    if not username:
        return None
    session = _open()
    if session is None:
        return None
    try:
        found = session.execute(
            text("SELECT principal_id FROM users WHERE username = :username"),
            {"username": username},
        ).scalar()
        return found if isinstance(found, str) and found else None
    except OperationalError:
        return None
    finally:
        session.close()


def note_terminal(run_id: str, outcome: str, summary: str) -> None:
    session = _open()
    if session is None:
        return
    try:
        if not trail_writable(session):
            return
        start = _start_payload(session, run_id)
        if start is None:
            return
        actor = start.get("actor") or SYSTEM_PRINCIPAL_ID
        trail.append(
            session,
            {
                "action": "agent.conclusion",
                "actor": actor,
                "run_id": run_id,
                "summary": summary,
            },
        )
        trail.append(
            session,
            {
                "action": "agent.outcome",
                "actor": actor,
                "run_id": run_id,
                "outcome": outcome,
            },
        )
        session.commit()
    except Exception:  # noqa: BLE001 - the run has already ended
        session.rollback()
        logger.exception("run %s ended without a trail outcome", run_id)
    finally:
        session.close()


def note_directive(
    session: Session,
    *,
    run_id: str,
    directive_id: str,
    kind: str,
    actor: str,
    actor_principal_id: Optional[str],
) -> None:
    principal = actor_principal_id or (
        SYSTEM_PRINCIPAL_ID if actor == "system" else None
    )
    if principal is None:
        return
    append_here(
        session,
        {
            "action": "agent.directive",
            "actor": principal,
            "run_id": run_id,
            "directive_id": directive_id,
            "kind": kind,
        },
    )


def note_tool(claims: Claims, tool: str, args: Any, *, outcome: Optional[str]) -> None:
    """Intent when ``outcome`` is None, the result entry otherwise.

    The payload names the tool and, when the call has one, its target.
    Arguments and the result body stay off it.
    """
    actor = phase_actor(claims.run_id or "") or claims.agent_id
    payload: dict[str, Any] = {
        "action": "agent.tool.intent" if outcome is None else "agent.tool.outcome",
        "actor": actor,
        "run_id": claims.run_id,
        "tool": tool,
    }
    if claims.person_id:
        payload["on_behalf_of"] = claims.person_id
    if claims.origin:
        payload["origin_kind"] = claims.origin
        payload["origin"] = (
            claims.person_id
            if claims.origin == "person" and claims.person_id
            else claims.origin
        )
    target = target_of(args)
    if target is not None:
        payload["target"] = target
    if outcome is not None:
        payload["outcome"] = outcome
    append_now(payload)


def phase_actor(run_id: str) -> Optional[str]:
    """The agent the playbook stored at start names for the running phase.

    The phase row's own agent text is not read: the body that reported the
    phase is not the source of the actor.
    """
    if not run_id:
        return None
    session = _open()
    if session is None:
        return None
    try:
        context = session.execute(
            text("SELECT trigger_context FROM workflow_runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar()
        if isinstance(context, str):
            context = json.loads(context)
        agents = context.get("phase_agents") if isinstance(context, dict) else None
        if not isinstance(agents, dict) or not agents:
            return None
        phase = session.execute(
            text(
                "SELECT phase_id FROM workflow_run_phases "
                "WHERE run_id = :run_id AND status = 'running' "
                "ORDER BY phase_order DESC LIMIT 1"
            ),
            {"run_id": run_id},
        ).scalar()
        if isinstance(phase, str) and isinstance(agents.get(phase), str):
            return agents[phase]
        if phase is None and len(agents) == 1:
            only = next(iter(agents.values()))
            return only if isinstance(only, str) else None
        return None
    except Exception:  # noqa: BLE001 - a missing row is not a phase agent
        return None
    finally:
        session.close()


def originating_person(session: Session, run_id: str) -> Optional[str]:
    """The person a run's start entry resolves to, walking parent origins."""
    seen: set[str] = set()
    current: Optional[str] = run_id
    while current and current not in seen:
        seen.add(current)
        start = _start_payload(session, current)
        if start is None:
            return None
        kind = start.get("origin_kind")
        origin = start.get("origin")
        if kind == "person" and isinstance(origin, str) and origin:
            return origin
        if kind == "parent" and isinstance(origin, str) and origin:
            current = origin
            continue
        return None
    return None


def _start_payload(session: Session, run_id: str) -> Optional[dict[str, Any]]:
    if not trail_writable(session):
        return None
    row = session.execute(
        text(
            "SELECT payload FROM audit_trail "
            "WHERE payload->>'action' = 'agent.run.start' "
            "  AND payload->>'run_id' = :run_id "
            "ORDER BY seq ASC LIMIT 1"
        ),
        {"run_id": run_id},
    ).scalar()
    if isinstance(row, str):
        row = json.loads(row)
    return row if isinstance(row, dict) else None


def column_present(session: Session, table: str, column: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table "
                "  AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
        is not None
    )
