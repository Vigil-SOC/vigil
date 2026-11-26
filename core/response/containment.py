"""An approved containment, carried out on the vendor's own ID (#5059, #5234).

A pending row of a kind in ``CONTAINMENT_TOOLS`` is resolved when it is read:
each connected server looks the target up, and what it finds is stored on
``parameters.containment`` as candidates. A keyless entry (the target is the
vendor's key: an address, a domain, an email) needs no lookup, only a target it
takes. The row's ``reversibility`` becomes the strictest stored class of the
candidates' acts. Approving names one;
its tool and arguments become ``parameters.tool``/``args`` and the call runs at
once through ``_run``, as the approver. The row records what the vendor answered
in ``execution_result.state``; a ``requested`` state is read again on read.

The way back (#5060): Release or Enable on an executed one inserts a row linked
by ``parameters.undoes``, approved by the person, carrying the table's undo tool
on the same vendor ID, or on a field of the act's answer (a Cloudflare rule id).
A carrier with no undo (an Access revoke) has no way back. It runs through ``carry_out`` like any approval; once the
vendor reports it, the original records ``released`` or ``enabled`` too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.response.approval_service import (
    CONTAINMENT_TYPES,
    ActionStatus,
    ApprovalService,
    PendingAction,
    runs_here,
    screen_actions,
    stored_reversibility,
)
from core.response.ceiling import IRREVERSIBLE, REVERSIBLE, STOPPED, tool_classes
from core.response.proposal_targets import (
    CONTAINMENT_TOOLS,
    DONE_STATE,
    UNDO_VERBS,
    UNDONE_LOOKUP,
    UNDONE_STATE,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

REQUESTED = "requested"
FAILED = "failed"
# A target no server found is looked up again at most this often, not on every read.
_RETRY = timedelta(seconds=60)
_MAX_ROWS = 50
_ACT_TIMEOUT_MS = 30_000
# Lookups and state reads run inside a read of the queue: kept short.
_READ_TIMEOUT_MS = 10_000
# A requested row's state is read from the vendor at most this often per process.
_REREAD_SECONDS = 10.0
_last_read: Dict[str, float] = {}
# An enabled vendor server this process has not connected is dialled at most this often.
_DIAL_SECONDS = 60.0
_DIAL_TIMEOUT_S = 10.0
_dialled: Dict[str, float] = {}
# Who approves and runs a row the ceiling released: the trail names it, not a person.
CEILING_APPROVER = "autonomy ceiling"
# The ceiling's hourly cap counts its releases over this window (#5274).
CAP_WINDOW = timedelta(minutes=60)
AUTONOMY_SCREEN = "/settings?section=autoinvestigate"


class Unapprovable(ValueError):
    """The approval cannot name a call: nothing resolved, or the pick is wrong."""


def _flat(entry: Dict[str, Any], key: str) -> str:
    return f"{entry['vendor']}_{entry[key]}"


def _entry(kind: str, vendor: Any) -> Optional[Dict[str, Any]]:
    return next(
        (e for e in CONTAINMENT_TOOLS.get(kind, ()) if e["vendor"] == vendor), None
    )


def _undoes(action: PendingAction) -> Optional[str]:
    return (action.parameters or {}).get("undoes") or None


def _as_state(action: PendingAction, state: str) -> str:
    """A vendor's state word, as this row records it: an undo row's act reads
    as done when its vendor reports the original's state gone."""
    if _undoes(action) and state == DONE_STATE.get(action.action_type):
        return UNDONE_STATE[action.action_type]
    return state


def _lookup_done(action: PendingAction) -> str:
    """The lookup's state word once this row's act holds."""
    if _undoes(action):
        return UNDONE_LOOKUP[action.action_type]
    return DONE_STATE[action.action_type]


async def _dial_vendors() -> None:
    """Connect the containment servers this process has not: a server enabled in
    another API worker is otherwise not seen here until a restart.
    ``connect_to_server`` skips one that is disabled or has no credentials."""
    from core.integrations.mcp.client import process_mcp_client

    client = process_mcp_client()
    if client is None:
        return
    connected = getattr(client, "tools_cache", None) or {}
    now = time.monotonic()
    for vendor in {e["vendor"] for kind in CONTAINMENT_TOOLS.values() for e in kind}:
        if vendor in connected or now - _dialled.get(vendor, -_DIAL_SECONDS) < (
            _DIAL_SECONDS
        ):
            continue
        _dialled[vendor] = now
        try:
            await asyncio.wait_for(
                client.connect_to_server(vendor, persistent=True), _DIAL_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — resolution says the tool is missing
            logger.info("containment server %s not connected", vendor)


async def live_registry(registry=None):
    """The registry, refreshed from the running MCP client."""
    from core.integrations.mcp.registry import MCPRegistry, refresh_from_client

    registry = registry or MCPRegistry()
    await _dial_vendors()
    try:
        refresh_from_client(registry)  # in-memory: reads the client's cache
    except Exception:  # noqa: BLE001 — a stale registry still answers
        logger.warning("MCP registry not refreshed", exc_info=True)
    return registry


async def _call(
    registry,
    tool: str,
    args: Dict[str, Any],
    action_id: Optional[str] = None,
    timeout_ms: int = _READ_TIMEOUT_MS,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(the tool's answer, None) or (None, why it failed), through ``_run``."""
    from core.agents.attribution import TrailWriteError
    from core.agents.mcp_tools import MCPFailure, row_error
    from core.agents.tools_router import (
        Bounds,
        Denied,
        InvokeRequest,
        _errored,
        _rows,
        _run,
    )

    body = InvokeRequest(
        tool=tool,
        args=args,
        bounds=Bounds(max_rows=_MAX_ROWS, timeout_ms=timeout_ms),
        action_id=action_id,
    )
    try:
        result, handled, _source = await _run(body, registry)
    except Denied as exc:
        return None, str(exc)
    except MCPFailure as exc:
        return None, exc.detail
    except asyncio.TimeoutError:
        return None, f"{tool} timed out"
    except TrailWriteError:
        return None, "the action could not be recorded"
    except Exception as exc:  # noqa: BLE001 — the row records what went wrong
        logger.exception("%s failed", tool)
        return None, str(exc) or type(exc).__name__
    if not handled:
        return None, f"no such tool: {tool}"
    rows = _rows(result)
    error = _errored(result) or row_error(rows)
    if error:
        return None, error
    answer = next((r for r in rows if isinstance(r, dict)), {})
    return answer, None


# ---- Resolution -----------------------------------------------------------


def _offered(kind: str, registry) -> List[Dict[str, Any]]:
    """Entries whose tools are connected: a keyless one needs only its act."""
    names = set(registry.get_tool_names())
    return [
        e
        for e in CONTAINMENT_TOOLS.get(kind, ())
        if (not e["lookup"] or _flat(e, "lookup") in names) and _flat(e, "act") in names
    ]


def _candidate(
    entry: Dict[str, Any], action: PendingAction, vendor_id: Any, name: Any, state
) -> Dict[str, Any]:
    return {
        "id": f"{entry['vendor']}:{vendor_id}",
        "vendor": entry["vendor"],
        "carrier": entry.get("carrier"),
        "tool": _flat(entry, "act"),
        "args": {entry["id_arg"]: vendor_id, entry["note_arg"]: _note(action)},
        "vendor_id": vendor_id,
        "name": name,
        "state": state,
        # Whether Vigil can undo the act: the carrier has an undo (#5234).
        "undoable": bool(entry.get("undo")),
    }


def _reversibility(candidates: List[Dict[str, Any]]) -> Optional[str]:
    """The strictest stored class of the candidates' acts; unclassified counts
    as irreversible. None when nothing resolved."""
    tools = {c["tool"] for c in candidates}
    if not tools:
        return None
    classes = tool_classes(tools)
    return (
        IRREVERSIBLE
        if any(stored_reversibility(classes.get(t)) == IRREVERSIBLE for t in tools)
        else REVERSIBLE
    )


def _due(action: PendingAction, offered: List[Dict[str, Any]]) -> bool:
    """No candidates yet (looked up again after _RETRY), or a server joined."""
    stored = (action.parameters or {}).get("containment")
    if not isinstance(stored, dict):
        return True
    if {e["vendor"] for e in offered} - set(stored.get("servers") or []):
        return True
    if stored.get("candidates"):
        return False
    if not offered:
        return False
    try:
        last = datetime.fromisoformat(stored.get("resolved_at") or "")
    except ValueError:
        return True
    return utcnow() - last.replace(tzinfo=None) >= _RETRY


def _records(entry: Dict[str, Any], answer: Dict[str, Any]) -> List[Dict[str, Any]]:
    if entry["rows"]:
        listed = answer.get(entry["rows"])
        return (
            [r for r in listed if isinstance(r, dict)]
            if isinstance(listed, list)
            else []
        )
    return [] if answer.get("found") is False else [answer]


def _note(action: PendingAction) -> str:
    return f"Vigil {action.action_id}: {action.title}"[:500]


async def resolve(action: PendingAction, registry) -> PendingAction:
    """Look the target up on every connected server that can act on it, and
    store what was found. A row that is not a pending containment is returned
    as it is."""
    if action.status != ActionStatus.PENDING.value:
        return action
    if action.action_type not in CONTAINMENT_TYPES:
        return action
    offered = _offered(action.action_type, registry)
    if not _due(action, offered):
        return action
    candidates: List[Dict[str, Any]] = []
    problems: List[str] = []
    refusals: List[str] = []
    for entry in offered:
        if not entry["lookup"]:
            # The target is the vendor's key: one candidate, if the act takes it.
            refusal = entry.get("takes") and entry["takes"](action.target)
            if refusal:
                refusals.append(refusal)
            else:
                candidates.append(
                    _candidate(entry, action, action.target, action.target, None)
                )
            continue
        lookup = _flat(entry, "lookup")
        answer, error = await _call(
            registry, lookup, entry["lookup_args"](action.target)
        )
        if error:
            problems.append(f"{lookup}: {error}")
            continue
        for record in _records(entry, answer or {}):
            # Carbon Black's device ID is a number: kept as the vendor sent it.
            vendor_id = record.get(entry["id"])
            if isinstance(vendor_id, bool) or vendor_id in (None, ""):
                continue
            if not isinstance(vendor_id, (str, int)):
                continue
            candidates.append(
                _candidate(
                    entry,
                    action,
                    vendor_id,
                    record.get(entry["name"]) or action.target,
                    entry["state"](record),
                )
            )
    missing = None
    if not offered:
        tools = [
            _flat(e, "lookup" if e["lookup"] else "act")
            for e in CONTAINMENT_TOOLS[action.action_type]
        ]
        missing = (
            "No connected integration can carry this: none offers "
            f"{' or '.join(tools)}"
        )
    elif not candidates:
        found_nothing = " or ".join(_flat(e, "lookup") for e in offered if e["lookup"])
        missing = "; ".join(refusals + problems) or (
            f"{found_nothing} found no match for {action.target}"
        )
    stored = {
        "candidates": candidates,
        "servers": [e["vendor"] for e in offered],
        "missing": missing,
        "resolved_at": utcnow().isoformat(),
    }
    reversibility = await asyncio.to_thread(_reversibility, candidates)
    updated = await asyncio.to_thread(
        ApprovalService().update_row,
        action.action_id,
        ActionStatus.PENDING.value,
        {"containment": stored},
        reversibility=reversibility,
    )
    return updated or action


def choose(action: PendingAction, candidate: Optional[str]) -> Tuple[str, str]:
    """The tool and JSON arguments approving ``action`` runs, or ``Unapprovable``."""
    stored = (action.parameters or {}).get("containment") or {}
    candidates = stored.get("candidates") or []
    if not candidates:
        raise Unapprovable(
            stored.get("missing") or "The target has not been resolved to a vendor ID"
        )
    if candidate is None:
        if len(candidates) > 1:
            ids = ", ".join(c["id"] for c in candidates)
            raise Unapprovable(f"{len(candidates)} matches; name one candidate: {ids}")
        chosen = candidates[0]
    else:
        chosen = next((c for c in candidates if c.get("id") == candidate), None)
        if chosen is None:
            raise Unapprovable(f"{candidate} is not one of this action's candidates")
    return chosen["tool"], json.dumps(chosen["args"], sort_keys=True)


# ---- Running the act and reading its state ---------------------------------


def _vendor_of(params: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """The candidate the row's tool came from: (vendor, vendor_id)."""
    tool = params.get("tool")
    for candidate in (params.get("containment") or {}).get("candidates") or []:
        if candidate.get("tool") == tool and _args_of(params) == candidate.get("args"):
            return candidate.get("vendor"), candidate.get("vendor_id")
    return None, None


def _args_of(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = json.loads(params.get("args") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def carry_out(action: PendingAction, claims, registry) -> PendingAction:
    """Run an approved, unheld containment's call as ``claims``' person and record
    what the vendor answered. Anything else is returned as it is.

    The row is claimed first, so a release racing an approval runs it once.
    """
    params = action.parameters or {}
    if action.action_type not in CONTAINMENT_TYPES or not runs_here(params):
        return action
    service = ApprovalService()
    claimed = await asyncio.to_thread(
        service.claim_run, action.action_id, claims.username or CEILING_APPROVER
    )
    if claimed is None:
        return action
    try:
        return await _carry_out(claimed, claims, registry, service)
    except Exception as exc:  # noqa: BLE001 — never leave the claim standing
        logger.exception("containment %s not carried out", action.action_id)
        failed = await asyncio.to_thread(
            service.mark_failed,
            action.action_id,
            str(exc) or type(exc).__name__,
            dict(
                zip(("vendor", "vendor_id"), _vendor_of(params)),
                state=FAILED,
                tool=params.get("tool"),
            ),
        )
        return failed or action


async def _carry_out(
    action: PendingAction, claims, registry, service: ApprovalService
) -> PendingAction:
    from core.auth.tool_principal import bound_claims
    from core.integrations.mcp.surface import acting_as
    from core.response.approval_decisions import record_execution

    params = action.parameters or {}
    tool, args = params["tool"], _args_of(params)
    vendor, vendor_id = _vendor_of(params)
    with bound_claims(claims), acting_as(claims.username):
        answer, error = await _call(
            registry, tool, args, action.action_id, _ACT_TIMEOUT_MS
        )
    # A run no person started acts under the ceiling, and the trail says so.
    by = claims.username or CEILING_APPROVER
    base = {"tool": tool, "vendor": vendor, "vendor_id": vendor_id, "run_by": by}
    if error == STOPPED:
        # Stopped after the approval: held until a person releases it.
        held = await asyncio.to_thread(service.unclaim_run, action.action_id, True)
        return held or action
    if error:
        updated = await asyncio.to_thread(
            service.mark_failed, action.action_id, error, {**base, "state": FAILED}
        )
    else:
        entry = _entry(action.action_type, vendor)
        state = entry["act_state"](answer or {}) if entry else REQUESTED
        # A vendor that answers the act at once (Defender's machine action).
        state = _as_state(action, state) if entry else state
        result = {**base, "state": state, "answer": answer}
        if state == FAILED:
            updated = await asyncio.to_thread(
                service.mark_failed,
                action.action_id,
                f"{tool} answered {json.dumps(answer)[:500]}",
                result,
            )
        else:
            updated = await asyncio.to_thread(
                service.mark_executed, action.action_id, result
            )
    if updated is None or updated.status not in ("executed", "failed"):
        return updated or action
    record_execution(updated, by)
    await _mark_undone(updated)
    return await read_state(updated, registry)


async def _mark_undone(action: PendingAction) -> None:
    """An undo row that reached its state: the original records it too."""
    original = _undoes(action)
    state = (action.execution_result or {}).get("state")
    if not original or state != UNDONE_STATE.get(action.action_type):
        return
    await asyncio.to_thread(
        ApprovalService().update_row,
        original,
        ActionStatus.EXECUTED.value,
        None,
        {
            "state": state,
            "undone_by": action.action_id,
            "undone_at": utcnow().isoformat(),
        },
    )


async def read_state(action: PendingAction, registry) -> PendingAction:
    """Read a ``requested`` containment's state from the vendor again, and record
    it when it changed. Anything else is returned as it is."""
    from core.response.approval_decisions import record_execution

    result = action.execution_result or {}
    if action.status != ActionStatus.EXECUTED.value or result.get("state") != REQUESTED:
        return action
    entry = _entry(action.action_type, result.get("vendor"))
    # A keyless act with no state read has nothing to read again: its answer is final.
    if entry is None or not (entry["state_read"] or entry["lookup"]):
        return action
    now = time.monotonic()
    if now - _last_read.get(action.action_id, 0.0) < _REREAD_SECONDS:
        return action
    _last_read[action.action_id] = now
    state: Optional[str] = None
    if entry["state_read"]:
        tool, arg = entry["state_read"]
        key = (result.get("answer") or {}).get(arg)
        if not key:
            return action
        answer, error = await _call(registry, f"{entry['vendor']}_{tool}", {arg: key})
        if error:
            logger.info("state of %s not read: %s", action.action_id, error)
            return action
        state = _as_state(action, entry["read_state"](answer or {}))
    else:
        answer, error = await _call(
            registry, _flat(entry, "lookup"), entry["lookup_args"](action.target)
        )
        if error:
            logger.info("state of %s not read: %s", action.action_id, error)
            return action
        record = next(
            (
                r
                for r in _records(entry, answer or {})
                if r.get(entry["id"]) == result.get("vendor_id")
            ),
            None,
        )
        if record is not None and entry["state"](record) == _lookup_done(action):
            state = _as_state(action, DONE_STATE[action.action_type])
    if state is None or state == REQUESTED:
        return action
    service = ApprovalService()
    if state == FAILED:
        updated = await asyncio.to_thread(
            service.mark_failed,
            action.action_id,
            f"{entry['vendor']} reports the act failed: {json.dumps(answer)[:500]}",
            {**result, "state": FAILED, "read": answer},
        )
    else:
        updated = await asyncio.to_thread(
            service.update_row,
            action.action_id,
            ActionStatus.EXECUTED.value,
            None,
            {"state": state, "read": answer, "read_at": utcnow().isoformat()},
        )
    if updated is None:
        return action
    record_execution(updated, result.get("run_by"))
    await _mark_undone(updated)
    return updated


async def _refresh(action: PendingAction, registry) -> PendingAction:
    try:
        action = await resolve(action, registry)
        return await read_state(action, registry)
    except Exception:  # noqa: BLE001 — the read stands without the refresh
        logger.exception("containment %s not refreshed", action.action_id)
        return action


def _readable(actions: List[PendingAction]) -> List[PendingAction]:
    """Of ``actions``, the containments the caller may read: no lookup runs for
    a row on a restricted case they can't."""
    actions = [a for a in actions if a.action_type in CONTAINMENT_TYPES]
    if not actions:
        return []
    shown = screen_actions([*actions])
    return [a for a, s in zip(actions, shown) if not s.restricted]


async def on_read(action: Optional[PendingAction], registry) -> Optional[PendingAction]:
    """Resolve a pending containment, or re-read a requested one, as it is read."""
    if action is None or action.action_type not in CONTAINMENT_TYPES:
        return action
    try:
        readable = await asyncio.to_thread(_readable, [action])
    except Exception:  # noqa: BLE001 — the read stands without the refresh
        logger.exception("containment %s not screened", action.action_id)
        return action
    return await _refresh(action, registry) if readable else action


async def refresh_open(registry) -> None:
    """Resolve pending containments and re-read requested ones, for a list read.
    The rows are refreshed side by side, each read bounded by _READ_TIMEOUT_MS."""
    service = ApprovalService()
    try:
        pending = await asyncio.to_thread(service.list_actions, ActionStatus.PENDING)
        executed = await asyncio.to_thread(service.list_actions, ActionStatus.EXECUTED)
        rows = await asyncio.to_thread(
            _readable,
            [
                a
                for a in pending + executed
                if a.status == ActionStatus.PENDING.value
                or (a.execution_result or {}).get("state") == REQUESTED
            ],
        )
    except Exception:  # noqa: BLE001 — the queue is read without the refresh
        logger.exception("containments not refreshed")
        return
    await asyncio.gather(*(_refresh(action, registry) for action in rows))


# ---- The way back (#5060) --------------------------------------------------


def _lift(original: PendingAction, offered: set) -> Any:
    """The undo row's title and parameters for ``original``, or why there is none."""
    result = original.execution_result or {}
    vendor, vendor_id = result.get("vendor"), result.get("vendor_id")
    entry = _entry(original.action_type, vendor)
    if entry is None or vendor_id in (None, ""):
        return f"{original.action_id} records no vendor ID to undo"
    if not entry.get("undo"):
        return f"{entry.get('carrier') or vendor} has no undo: Vigil cannot undo this"
    tool = _flat(entry, "undo")
    if tool not in offered:
        return f"No connected server offers {tool}"
    verb = UNDO_VERBS[original.action_type]
    if entry.get("undo_from"):
        # The undo acts on what the act returned (Cloudflare's rule id).
        field = entry["undo_from"]
        key = (result.get("answer") or {}).get(field)
        if key in (None, ""):
            return f"{original.action_id} records no {field} to undo"
        args = {field: key}
    else:
        args = {
            entry["id_arg"]: vendor_id,
            entry["note_arg"]: f"Vigil: {verb} {original.target}, undoing "
            f"{original.action_id}"[:500],
        }
    stored = (original.parameters or {}).get("containment") or {}
    name = next(
        (
            c.get("name")
            for c in stored.get("candidates") or []
            if c.get("vendor") == vendor and c.get("vendor_id") == vendor_id
        ),
        original.target,
    )
    candidate = {
        "id": f"{vendor}:{vendor_id}",
        "vendor": vendor,
        "tool": tool,
        "args": args,
        "vendor_id": vendor_id,
        "name": name,
        "carrier": entry.get("carrier"),
        "state": DONE_STATE[original.action_type],
    }
    return f"{verb} {name}", {
        "tool": tool,
        "args": json.dumps(args, sort_keys=True),
        "containment": {"candidates": [candidate], "servers": [vendor]},
    }


async def undo(
    action_id: str, claims, registry, reason: Optional[str] = None
) -> PendingAction:
    """Release, enable or unblock what ``action_id`` contained, as ``claims``' person: the
    approved undo row, run at once (held when stopped), with their ``reason``.
    Raises ``NotUndoable``."""
    registry = await live_registry(registry)
    offered = set(registry.get_tool_names())
    person = getattr(claims, "person_id", None)
    row = await asyncio.to_thread(
        ApprovalService().create_undo,
        action_id,
        lambda original: _lift(original, offered),
        claims.username,
        person,
        reason,
    )
    if row.held:
        return row
    return await carry_out(row, claims, registry)


# ---- Under the autonomy ceiling (#5235) ------------------------------------


def unattended(claims) -> Optional[Any]:
    """The run's own claims with no person on them, when ``claims`` are a run's
    that no person started; None otherwise (a chat turn, a person's run, none)."""
    from core.auth.tool_principal import Claims

    if claims is None or not claims.run_id:
        return None
    if claims.username or claims.person_id or claims.origin == "person":
        return None
    return Claims(
        username=None,
        person_id=None,
        agent_id=claims.agent_id,
        run_id=claims.run_id,
        origin=claims.origin,
    )


async def release_under_ceiling(
    action: PendingAction, claims, registry=None
) -> PendingAction:
    """Carry out a containment newly proposed by a run no person started, when
    its row resolves to one candidate, four-eyes is off and the ceiling lets that
    run take the act. Otherwise, or on any failure, the row stays pending."""
    if (
        claims is None
        or claims.username
        or action.status != ActionStatus.PENDING.value
        or action.action_type not in CONTAINMENT_TYPES
    ):
        return action
    try:
        return await _release(action, claims, registry)
    except Exception:  # noqa: BLE001 — the proposal stands for a person
        logger.exception("%s not released under the ceiling", action.action_id)
        return action


async def _release(action: PendingAction, claims, registry) -> PendingAction:
    from core.auth.principals import SYSTEM_PRINCIPAL_ID
    from core.auth.tool_principal import bound_claims
    from core.response.ceiling import read_ceiling, unattended_refusal
    from core.response.reversals import PersonHold

    registry = await live_registry(registry)
    action = await resolve(action, registry)
    candidates = ((action.parameters or {}).get("containment") or {}).get(
        "candidates"
    ) or []
    ceiling = await asyncio.to_thread(read_ceiling)
    why = None
    if len(candidates) != 1:
        why = f"{len(candidates)} candidates"
    elif ceiling.four_eyes:
        why = "four-eyes is on"
    if why:
        logger.info("%s waits for a person: %s", action.action_id, why)
        return action
    call = choose(action, None)
    # The vendor's ID alone names no crown jewel: the target and name go too.
    args = {
        **candidates[0]["args"],
        "target": action.target,
        "name": candidates[0].get("name"),
    }
    with bound_claims(claims):
        # The rule _run applies to the act, asked first so a refused row stays pending.
        why = await asyncio.to_thread(
            unattended_refusal, call[0], writes=True, run_id=claims.run_id, args=args
        )
        if why:
            logger.info("%s waits for a person: %s", action.action_id, why)
            if isinstance(why, PersonHold):
                await asyncio.to_thread(_note_held, action, why, claims.run_id)
            return action
        # Last, so the case names the cap only when nothing else held the act.
        released = await asyncio.to_thread(released_last_hour)
        if released >= ceiling.released_per_hour:
            logger.info(
                "%s waits for a person: hourly cap: %d of %d released",
                action.action_id,
                released,
                ceiling.released_per_hour,
            )
            await asyncio.to_thread(
                _note_capped, action, released, ceiling.released_per_hour
            )
            return action
        approved = await asyncio.to_thread(
            ApprovalService().approve_action,
            action.action_id,
            CEILING_APPROVER,
            SYSTEM_PRINCIPAL_ID,
            call,
        )
    if approved is None:
        return action
    await asyncio.to_thread(_note_released, approved, claims.run_id)
    return await carry_out(approved, claims, registry)


def _note_held(action: PendingAction, why: str, run_id: str) -> None:
    """Say on the case which person's decision keeps the row pending (#5273).
    Never raises: the row waits for a person either way."""
    from core.response.approval_service import case_ids_for
    from core.workflows.run_bridge_router import _add_activity

    try:
        for case_id in case_ids_for(action):
            _add_activity(
                case_id,
                "approval_held",
                f"{action.title} waits for a person: {why}",
                {"action_id": action.action_id, "run_id": run_id, "held_by": why},
            )
    except Exception:  # noqa: BLE001 — the row waits either way
        logger.exception("hold of %s not noted on its case", action.action_id)


def _note_released(action: PendingAction, run_id: str) -> None:
    """Never raises: the row is approved, and carry_out must still run it."""
    from core.response.approval_service import case_ids_for
    from core.workflows.run_bridge_router import _add_activity

    try:
        for case_id in case_ids_for(action):
            _add_activity(
                case_id,
                "approval_approved",
                f"{action.title} released by the autonomy ceiling for run {run_id},"
                " which no person started",
                {
                    "action_id": action.action_id,
                    "decided_by": CEILING_APPROVER,
                    "run_id": run_id,
                    "status": action.status,
                },
            )
    except Exception:  # noqa: BLE001 — the act runs either way
        logger.exception("release of %s not noted on its case", action.action_id)


def released_last_hour() -> int:
    """Rows the ceiling approved in the last CAP_WINDOW, whatever happened to them since."""
    from sqlalchemy import func, select

    from core.storage.connection import get_db_manager
    from core.storage.models import ApprovalAction

    with get_db_manager().session_scope() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(ApprovalAction)
                .where(
                    ApprovalAction.approved_by == CEILING_APPROVER,
                    ApprovalAction.approved_at >= utcnow() - CAP_WINDOW,
                )
            )
            or 0
        )


def _note_capped(action: PendingAction, released: int, cap: int) -> None:
    """Never raises: the row waits for a person either way."""
    from core.response.approval_service import case_ids_for
    from core.workflows.run_bridge_router import _add_activity

    try:
        for case_id in case_ids_for(action):
            _add_activity(
                case_id,
                "approval_held",
                f"{action.title} waits for a person: "
                + (
                    f"the autonomy ceiling released {released} in the last hour,"
                    f" at its hourly cap of {cap}"
                    if cap
                    else "the autonomy ceiling's hourly cap is 0, so it releases none"
                ),
                {
                    "action_id": action.action_id,
                    "released": released,
                    "released_per_hour": cap,
                    "status": action.status,
                },
            )
    except Exception:  # noqa: BLE001 — the row stays pending either way
        logger.exception("hold of %s not noted on its case", action.action_id)


def cap_reached(user) -> list[dict[str, str]]:
    """The bell's warning while the hourly cap is reached, for whoever may raise it.
    A cap of 0 is a choice, not a warning."""
    from core.auth.permissions import missing_permission
    from core.response.ceiling import read_ceiling

    if missing_permission(user.user_id, "ai_autonomy.write") is not None:
        return []
    cap = read_ceiling().released_per_hour
    released = released_last_hour() if cap else 0
    if not cap or released < cap:
        return []
    return [
        {
            "message": f"The autonomy ceiling has released {released} "
            f"action{'' if released == 1 else 's'} in the last hour, at its cap of "
            f"{cap}. New proposals from runs no person "
            "started wait for a person until the count falls.",
            "link": AUTONOMY_SCREEN,
        }
    ]


def register() -> None:
    from core.platform import notices

    notices.CONDITIONS.append(cap_reached)
