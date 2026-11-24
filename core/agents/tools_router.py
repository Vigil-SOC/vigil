# The one way the TypeScript agent layer reaches a Python tool. Bounds are applied
# here rather than after serialisation, which would malform the result.

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import nullcontext
from typing import Any, ContextManager, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from core.agents.attribution import (
    TrailWriteError,
    append_now,
    attributed,
    bound_claims,
    current_claims,
    effectful,
    note_tool,
)
from core.agents.integration_tools import resolve_integration_call
from core.agents.internal_auth import accepted, authorise, authorise_run
from core.agents.mcp_tools import MCPFailure, execute_mcp_tool, split_tool_name
from core.agents.search_ceiling import SearchAfterClose, cap_search_args
from core.agents.tool_registry import (
    MANIFEST,
    execute_backend_tool,
    required_permission,
)
from core.auth import sign_ins, tool_principal
from core.auth.permissions import permission_refusal
from core.auth.principals import SYSTEM_PRINCIPAL_ID
from core.auth.tool_principal import Claims
from core.cases.case_access import lost_reader
from core.deps import provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry
from core.integrations.mcp.surface import acting_as
from core.llm.tool_schemas import CALL_INTEGRATION_TOOL
from core.response import emulation
from core.response.approval_service import ActionStatus, ApprovalService
from core.response.ceiling import (
    STOPPED,
    execution_held,
    gated_tools,
    unattended_refusal,
)
from core.routing import Auth, RouterMeta
from core.skills.skill_library import READ_SKILL_TOOL
from core.skills.skill_usage import record_skill_read
from core.workflows.redecision_hold import redecision_close

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/internal/tools",
    tags=["internal-tools"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A shared secret: the caller is the agent layer, not a session. Reachability\n"
        "is the NetworkPolicy's job since ADR 0014, not a loopback check."
    ),
)
logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "vigil"


class Bounds(BaseModel):
    max_rows: int = Field(..., gt=0)
    timeout_ms: int = Field(..., gt=0)


class InvokeRequest(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    bounds: Bounds
    # An API-signed token for the session's user (core/auth/tool_principal.py) and
    # ToolPrincipal in contracts/tool.ts: a person's, or a run's with no username
    # when no person started it. Absent is refused (#3306).
    principal: Optional[str] = None
    # The Vigil agent this call runs for, when one does (#1560). It rides the
    # turn rather than the harness because a compose run shares one harness
    # across phases run by different agents. Absent means unattributed: a
    # skill read is still counted, with no agent to credit.
    agent_id: Optional[str] = None
    # Set when the agent layer is running a call a person already approved.
    # A direct-action tool is refused without them: the token alone is not
    # an approval, and the row is what names the call and its arguments.
    run_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    # Or an approved row that names this call itself: a containment (#5059).
    action_id: Optional[str] = None


def _failure(kind: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "failure": {"kind": kind, **detail}}


async def _note_skill_read(body: InvokeRequest) -> None:
    """Count one skill use (#1560), after the read is known to have succeeded.

    Only a read of the skill body counts: a follow-up read of a supporting
    file is the same use, not another one. Runs with no agent behind them
    record an unattributed read rather than an invented id. A failed insert
    is logged and swallowed — the usage log must never fail the tool call it
    is counting — and the write runs off the event loop.
    """
    if body.tool != READ_SKILL_TOOL or body.args.get("file"):
        return
    name = body.args.get("name")
    if not isinstance(name, str) or not name:
        return
    try:
        await asyncio.to_thread(record_skill_read, name, body.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("recording the read of skill %s failed", name)


# Where a tool that answers an envelope keeps the rows. Read as one object the whole
# envelope is one row, so max_rows never bites however much came back.
_ENVELOPE_ROWS = ("results", "rows", "events", "items", "data")


# Whatever the ladder returned, as rows. A bare mapping is one row rather than no
# rows, so a tool answering with a single object is not read as an empty result.
def _rows(result: Any) -> List[Any]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        # An envelope reporting a failure is not an empty result: unwrapping it to its
        # own empty rows list reads as "the query ran and found nothing", which is the
        # one answer a hunt must not confuse with "the query could not be run".
        if result.get("error"):
            return [result]
        for field in _ENVELOPE_ROWS:
            held = result.get(field)
            if isinstance(held, list):
                return held
    return [result]


# The ladder reports a failure in-band rather than raising, so that shape is read
# back out here.
def _errored(result: Any) -> Optional[str]:
    if isinstance(result, dict) and isinstance(result.get("error"), str):
        return result["error"]
    return None


# Python's wording for a call that did not fit its signature. A TypeError from
# inside a tool is not one, and invalid_args tells the model to retry until the cap.
_SIGNATURE_MISMATCH = (
    "unexpected keyword argument",
    "required positional argument",
    "required keyword-only argument",
    "positional argument",
)


def _is_bad_arguments(exc: TypeError) -> bool:
    return any(phrase in str(exc) for phrase in _SIGNATURE_MISMATCH)


# The names a row cap travels under: "limit" alone misses splunk_execute's max_results,
# and the MCP servers are the ones that answer in bulk.
_ROW_CAP_ARGS = ("limit", "max_results", "max_count")


# The cap reaches the tool rather than only its answer, and a caller asking for less
# keeps its own number. What ignores the cap is still truncated below.
#
# Only names the call already carries are lowered: setting all of them would hand a
# tool a keyword its signature does not take. When the call names none, a cap is
# attached only if the tool's schema already declares one — get_finding takes
# finding_id alone, and injecting limit made every point-read invalid_args.
def _schema_row_caps(
    tool: str, registry: Optional["MCPRegistry"] = None
) -> Optional[Dict[str, Dict[str, Any]]]:
    """The row-cap arguments ``tool`` declares, each with its schema, or None if
    nothing describes it.

    MANIFEST covers the backend tools. An MCP tool is described by the registry
    instead, and looking only in MANIFEST reported every one of them as
    undescribed -- which is how ``limit`` came to be sent to tools that do not
    take one.
    """
    properties = _schema_properties(tool, registry)
    if properties is None:
        return None
    return {name: properties[name] for name in _ROW_CAP_ARGS if name in properties}


def _schema_properties(
    tool: str, registry: Optional["MCPRegistry"] = None
) -> Optional[Dict[str, Any]]:
    """The arguments ``tool``'s input schema declares, or None if nothing describes it."""
    spec = MANIFEST.get(tool)
    if spec is None and registry is not None:
        try:
            spec = next(
                (t for t in registry.get_all_tools() if t.get("name") == tool), None
            )
        except Exception:  # noqa: BLE001 - an unreadable registry describes nothing
            spec = None
    if spec is None:
        return None
    return (spec.get("input_schema") or spec.get("inputSchema") or {}).get(
        "properties"
    ) or {}


def _scoped(
    args: Dict[str, Any],
    tool: str,
    limits: "emulation.Emulation",
    registry: Optional["MCPRegistry"] = None,
) -> Dict[str, Any]:
    """An emulator's call as sent: the hostname in its listed spelling, and the
    listed hosts in ``allowed_hosts`` when the schema declares that argument, as
    ``_bounded`` fills a declared row cap. Never sent otherwise: it would be a
    keyword the tool does not take."""
    hostname = str(args.get("hostname") or "").strip()
    listed = limits.listed(hostname) if hostname else None
    scoped = {**args, "hostname": listed} if listed else {**args}
    declared = (_schema_properties(tool, registry) or {}).get("allowed_hosts")
    if declared is None:
        return scoped
    kind = declared.get("type") if isinstance(declared, dict) else None
    hosts = limits.hosts
    scoped["allowed_hosts"] = ", ".join(hosts) if kind == "string" else list(hosts)
    return scoped


# A server that bounds its own row cap refuses a call past it rather than answering
# fewer rows: mcp-shodan 1.0.34 takes max_results up to 100, and the 200 a chat turn
# carries made every shodan_search invalid.
def _ceiling(max_rows: int, schema: Optional[Dict[str, Any]]) -> int:
    maximum = (schema or {}).get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        return min(max_rows, int(maximum))
    return max_rows


def _bounded(
    args: Dict[str, Any],
    max_rows: int,
    tool: str,
    registry: Optional["MCPRegistry"] = None,
) -> Dict[str, Any]:
    declared = _schema_row_caps(tool, registry) or {}
    named = [name for name in _ROW_CAP_ARGS if name in args]
    if named:
        lowered = {}
        for name in named:
            ceiling = _ceiling(max_rows, declared.get(name))
            value = args[name]
            lowered[name] = min(value, ceiling) if isinstance(value, int) else ceiling
        return {**args, **lowered}

    if not declared:
        # Nothing describes a row cap on this tool, or it has none. Sending one
        # anyway is a keyword its signature does not take; what it answers is
        # still truncated below, so the cap is not lost by not being sent.
        return {**args}
    name = next(iter(declared))
    return {**args, name: _ceiling(max_rows, declared[name])}


# The telemetry plane the rows came out of, which is what a hunt counts corroboration
# over. One label for everything leaves a worker's own typed string as the only domain.
def _source_system(tool: str, registry: MCPRegistry) -> str:
    try:
        split = split_tool_name(tool, registry.get_active_servers())
    except Exception:  # noqa: BLE001 — a registry that cannot be read names no server
        return SOURCE_SYSTEM
    return SOURCE_SYSTEM if split is None else split[0]


def _norm_args(value: Any) -> Any:
    """A shape two JSON parsers can agree on.

    ``1`` and ``1.0`` are the same argument. A bool is not a number, and key
    order is not part of the call.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_norm_args(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _norm_args(item) for key, item in sorted(value.items())}
    return str(value)


def _args_match(stored: Any, sent: Dict[str, Any]) -> bool:
    if not isinstance(stored, str):
        return False
    try:
        parsed = json.loads(stored or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return _norm_args(parsed) == _norm_args(sent)


def _direct_action_refusal(
    body: InvokeRequest, tool: str, args: Dict[str, Any]
) -> Optional[str]:
    """Why this direct-action call may not run, or None when an approval covers it.

    The agent layer parks the call and a person approves that checkpoint. This
    repeats the check: a caller that holds the internal token and names the
    tool itself does not get to skip it. A reversible, irreversible or
    unclassified outside tool needs the approval; a read, and Vigil's own
    tools, are not this function's concern.
    """
    if tool not in gated_tools([tool]):
        return None
    if body.action_id:
        return _approved_row_refusal(body.action_id, tool, args)
    if not body.run_id or not body.checkpoint_id:
        return f"{tool} changes the estate and no approval covers this call"
    try:
        actions = ApprovalService().list_actions(
            status=ActionStatus.APPROVED, workflow_run_id=body.run_id
        )
    except Exception:  # noqa: BLE001 — unreadable is not approved
        logger.exception("could not read approvals for %s", body.run_id)
        return f"{tool} changes the estate and its approval could not be read"
    for action in actions:
        params = action.parameters or {}
        if str(params.get("checkpoint_id") or "") != body.checkpoint_id:
            continue
        if params.get("tool") != tool:
            continue
        if _args_match(params.get("args"), args):
            return None
    return f"{tool} changes the estate and no approval covers this call"


def _approved_row_refusal(
    action_id: str, tool: str, args: Dict[str, Any]
) -> Optional[str]:
    """None when ``action_id`` is an approved, unheld row naming this exact call,
    claimed for running (core/response/containment.py carry_out)."""
    try:
        action = ApprovalService().get_action(action_id)
    except Exception:  # noqa: BLE001 — unreadable is not approved
        logger.exception("could not read approval %s", action_id)
        return f"{tool} changes the estate and its approval could not be read"
    params = (action.parameters or {}) if action is not None else {}
    if (
        action is not None
        and action.status == ActionStatus.APPROVED.value
        and not action.held
        and (action.execution_result or {}).get("state") == "running"
        and not params.get("checkpoint_id")
        and params.get("tool") == tool
        and _args_match(params.get("args"), args)
    ):
        return None
    return f"{tool} changes the estate and no approval covers this call"


def _stopped(tool: str) -> bool:
    """A stop refuses every gated outside call, whoever makes it (#5059)."""
    return tool in gated_tools([tool]) and execution_held()


class Denied(Exception):
    """Not allowed for whom the call runs as. A run parks on the reason."""


def _denial(claims: Claims, tool: str, args: Dict[str, Any]) -> Optional[str]:
    """Why the call that runs may not run for whom it runs as, or None (#3306).

    A person is held to their live role, a run no person started to the
    autonomy ceiling. An integration call is checked as the tool it names.
    """
    permission = required_permission(tool)
    if claims.username:
        refusal = permission_refusal(permission) if permission else None
        return None if refusal is None else refusal["error"]
    writes = not (permission or "").endswith(".read")
    return unattended_refusal(tool, writes=writes, run_id=claims.run_id, args=args)


def _note_unbound(tool: str) -> None:
    """A call that names nobody has no claims to record it against."""
    try:
        append_now(
            {
                "action": "agent.tool.outcome",
                "actor": SYSTEM_PRINCIPAL_ID,
                "tool": tool,
                "outcome": "denied",
                "reason": "no principal",
            }
        )
    except TrailWriteError:
        logger.exception("refusal of unbound %s was not recorded", tool)


def _recording(tool: str, body: InvokeRequest) -> Optional[Claims]:
    """The claims an effectful, attributed call records. A read-only call has none.

    A person running an approved row is attributed to themselves.
    """
    claims = current_claims()
    if claims is None or not effectful(tool):
        return None
    if attributed(claims) or (body.action_id and claims.person_id):
        return claims
    return None


def _intent(claims: Claims, tool: str, args: Dict[str, Any]) -> None:
    note_tool(claims, tool, args, outcome=None)


def _outcome(claims: Claims, tool: str, args: Dict[str, Any], outcome: str) -> None:
    """The call has already happened. A missed outcome stays unknown."""
    try:
        note_tool(claims, tool, args, outcome=outcome)
    except TrailWriteError:
        logger.exception("tool %s outcome was not recorded", tool)


# Backend tools first, then the MCP servers. One ceiling governs both, so a tool
# does not get a second timeout by virtue of living on the other side.
#
# call_integration_tool stands for the integration call it names, so that call is
# what runs, bounded and attributed as if it had been declared itself.
async def _run(body: InvokeRequest, registry: MCPRegistry) -> Tuple[Any, bool, str]:
    seconds = body.bounds.timeout_ms / 1000
    tool, raw = body.tool, body.args
    if tool == CALL_INTEGRATION_TOOL:
        tool, raw = resolve_integration_call(registry, raw)
    claims = current_claims()
    # A marked emulator is held to the operator's host list before anything leaves.
    limits = await asyncio.to_thread(emulation.read_emulation)
    held = emulation.refusal(limits, tool, raw)
    if held is not None:
        if attributed(claims):
            _outcome(claims, tool, raw, "refused")
        raise MCPFailure("refused", held)
    args = _bounded(raw, body.bounds.max_rows, tool, registry)
    # A re-decision's searches end at the ticket's close. The close is read from
    # the run, not from the arguments. Live runs are not marked and pass through.
    if claims is not None and claims.run_id:
        close = await asyncio.to_thread(redecision_close, claims.run_id)
        if close is not None:
            try:
                args = cap_search_args(tool, args, close)
            except SearchAfterClose as exc:
                if attributed(claims):
                    _outcome(claims, tool, raw, "refused")
                raise MCPFailure("refused", str(exc)) from exc
    # Into what is sent only: an approval and the trail hold what the model asked.
    if limits.marked(tool):
        args = _scoped(args, tool, limits, registry)

    denial = None if claims is None else _denial(claims, tool, raw)
    # A run whose person can no longer read its own case parks on the reason
    # (#4358): it reads that case at every step, so its next call is refused.
    if denial is None and claims is not None:
        denial = await asyncio.to_thread(lost_reader)
    if denial is None and await asyncio.to_thread(_stopped, tool):
        denial = STOPPED
    if denial is not None:
        if attributed(claims) or _recording(tool, body) is not None:
            _outcome(claims, tool, raw, "denied")
        raise Denied(denial)

    record = _recording(tool, body)
    if record is not None:
        _intent(record, tool, raw)
    outcome = "failed"
    try:
        result, handled = await asyncio.wait_for(
            execute_backend_tool(tool, args, registry=registry), timeout=seconds
        )
        if handled:
            outcome = "failed" if _errored(result) is not None else "done"
            return result, True, SOURCE_SYSTEM
        # After the backend, so a Vigil tool is not caught by a verb in its name.
        # The arguments compared are the ones the model sent, before the row cap
        # lowered a limit the approval was not asked about. A run no person
        # started was already held to the ceiling above; nobody approves for it.
        person = claims is not None and bool(claims.username)
        refusal = _direct_action_refusal(body, tool, raw) if person else None
        if refusal is not None:
            outcome = "refused"
            raise MCPFailure("refused", refusal)
        result, handled = await asyncio.wait_for(
            execute_mcp_tool(tool, args, seconds, registry), timeout=seconds
        )
        if handled and limits.marked(tool):
            # The list bounds what Vigil asks, not what the far side does: a
            # step it reports on another host is flagged, the answer unchanged.
            stray = emulation.unlisted_hosts(limits, result)
            if stray:
                run_id = body.run_id or (claims.run_id if claims else None)
                await asyncio.to_thread(emulation.note_unlisted, tool, run_id, stray)
        if not handled:
            outcome = "refused"
        elif _errored(result) is not None:
            outcome = "failed"
        else:
            outcome = "done"
        return result, handled, _source_system(tool, registry)
    finally:
        if record is not None:
            _outcome(record, tool, raw, outcome)


class SkillReadRequest(BaseModel):
    name: str = Field(..., min_length=1)
    agent_id: Optional[str] = None


@router.post("/skill-reads", status_code=204)
async def note_skill_read(
    body: SkillReadRequest, authorization: Optional[str] = Header(default=None)
) -> None:
    """Count a skill a run loaded with load_skill, which the agent layer answers itself.

    The same count a read_skill call through ``/invoke`` makes, for the skill
    bodies a run reads without coming here. A failed insert is logged and
    swallowed, as there.
    """
    authorise(authorization, "skill read")
    try:
        await asyncio.to_thread(record_skill_read, body.name, body.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("recording the read of skill %s failed", body.name)


class SkillBodyRequest(BaseModel):
    name: str = Field(..., min_length=1)


def _operator_skill_body(name: str) -> Optional[str]:
    """The current text of the operator skill called ``name``, or None.

    Bundled skills are not served: a run carries those itself. A skill the scan
    holds is None too, the same as one that does not exist, so a run already
    going cannot read what a new one could not. Verdicts that cannot be read
    leave every skill pending, so a hold that cannot be decided is not a pass.
    """
    from core.skills.skill_hold import agent_skills
    from core.skills.skill_library import is_bundled, skill_body

    for skill in agent_skills():
        if skill.name == name and not is_bundled(skill):
            try:
                return skill_body(skill)
            except OSError:
                return None
    return None


@router.post("/skill-bodies")
async def read_skill_body(
    body: SkillBodyRequest, authorization: Optional[str] = Header(default=None)
) -> Dict[str, str]:
    """An operator skill's current body, for the load_skill a run answers itself.

    Runs carry a skill's name, description and digest, not its text, so the text
    is read here when it is asked for, and a resumed run reads what the skill says
    now. The answer is ``{"name", "body"}``; a refusal is an error status whose
    ``detail`` the agent layer hands the model.
    """
    authorise(authorization, "skill body")
    text = await asyncio.to_thread(_operator_skill_body, body.name)
    if text is None:
        raise HTTPException(status_code=404, detail=f"No skill named {body.name!r}")
    return {"name": body.name, "body": text}


@router.post("/invoke")
async def invoke(
    body: InvokeRequest,
    authorization: Optional[str] = Header(default=None),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> Dict[str, Any]:
    what = "tool invocation"
    # Settled after the run check, so a run-principal refusal keeps its latch
    # instead of being reset by the bearer succeeding on every call.
    authorise(authorization, what, note_ok=False)
    # A call that names nobody is refused, not parked: a run parked before
    # tokens existed comes back with none, and parking it again would loop.
    if body.principal is None:
        accepted(what)
        _note_unbound(body.tool)
        return _failure("refused", detail="no principal: the call names nobody")
    # A token that does not verify is refused, never read as "no person": that
    # would record a person's work as an agent's. A bad token is not a denial.
    try:
        claims = tool_principal.read(body.principal)
    except tool_principal.InvalidPrincipal:
        accepted(what)
        raise HTTPException(status_code=401, detail="bad or expired principal")
    # An approved row runs from its approval, in-process, never from here.
    if body.action_id is not None:
        accepted(what)
        return _failure("refused", detail="an approved action runs from its approval")
    # An approved action names its run. The principal has to name that same run.
    refusal = authorise_run(what, body.run_id, claims=claims)
    if refusal:
        return _failure("refused", detail=refusal)
    accepted(what)
    # A run whose sign-in ended for a stopping reason calls nothing more, whether
    # or not its worker has been told to stop yet. Other endings keep it working.
    if claims.sid and await asyncio.to_thread(
        sign_ins.ended_for_compromise, claims.sid
    ):
        return _failure("refused", detail=sign_ins.STOP_REASON)
    bound: ContextManager[None] = nullcontext()
    if claims.username:
        bound = acting_as(claims.username)

    try:
        # The tool runs in this context (or a copy of it), so it sees the binding.
        with bound_claims(claims), bound:
            result, handled, source = await _run(body, registry)
    except Denied as exc:
        return _failure("denied", detail=str(exc))
    except TrailWriteError:
        return _failure("backend_error", detail="the action could not be recorded")
    except asyncio.TimeoutError:
        return _failure("timeout", timeoutMs=body.bounds.timeout_ms)
    # An MCP server that could not be reached is a gap in visibility, not a defect
    # in the call, and the hunt records the two differently.
    except MCPFailure as exc:
        if exc.kind == "timeout":
            return _failure("timeout", timeoutMs=body.bounds.timeout_ms)
        return _failure(exc.kind, detail=exc.detail)
    except TypeError as exc:
        if _is_bad_arguments(exc):
            return _failure("invalid_args", detail=str(exc))
        logger.exception("tool %s failed", body.tool)
        return _failure("backend_error", detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool %s failed", body.tool)
        return _failure("backend_error", detail=str(exc))

    # refused is for a name nothing implements. A tool that ran and could not
    # answer is a backend_error: the contract keeps the two apart deliberately.
    if not handled:
        return _failure("refused", detail=f"no such tool: {body.tool}")
    errored = _errored(result)
    if errored is not None:
        return _failure("backend_error", detail=errored)

    await _note_skill_read(body)

    rows = _rows(result)
    capped = len(rows) > body.bounds.max_rows
    return {
        "ok": True,
        "rows": rows[: body.bounds.max_rows],
        "rowCount": min(len(rows), body.bounds.max_rows),
        "capped": capped,
        "sourceSystem": source,
    }
