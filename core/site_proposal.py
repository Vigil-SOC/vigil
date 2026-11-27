"""The site proposal and the site questions (#5229, epic #5218).

One open proposal of the whole ``site/`` folder, kept in one ``system_config``
row written directly, as ``core/detections/gap_run.py`` keeps its run state:
never through a config write, which would copy answer text into the config
audit. Each save replaces it; no history is kept. A person downloads the
folder and commits it; Vigil applies it as any revision (``core/site.py``).

Every item the folder adds, changes or removes against the applied revision
rests on provenance: a cited call on the calling run's own ledger, or a
person's answer in a chat turn. Crown jewels, owners and blind spots rest only
on a person's answer. Status and the diff are computed on read.

Site questions are computed on read and stored nowhere: what the SOC's runs
keep touching that the declaration does not cover. Parsing, matching and
owners are ``core/site.py``'s; nothing here parses a selector or matches a key.
"""

from __future__ import annotations

import io
import json
import logging
import re
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from core.site import (
    KINDS,
    SITE_DIR_NAME,
    SiteRejected,
    applied_site,
    empty_items,
    match,
    parse_site,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

PROPOSAL_KEY = "site.proposal"
# Serialises the save and the discard on the row, whether or not it exists yet.
_LOCK = 0x7669_6769_6C53_5052  # "vigilSPR"

# Site questions: defaults, not settings, as in gap_run.py.
WINDOW_DAYS = 30
# Distinct investigations an entity needs in the window before it is a question.
QUESTION_FLOOR = 2
# Lines per question kind, the most-touched first.
MAX_QUESTIONS = 200

SAVE_ACTION = "site.proposal.save"
DISCARD_ACTION = "site.proposal.discard"
REFRESH_ACTION = "site.proposal.refresh"

# The field that names an item of each list in the diff and in provenance.
LABEL = {
    "zones": "name",
    "crown_jewels": "name",
    "owners": "team",
    "blind_spots": "what",
    "telemetry": "name",
}
# Lists whose items only a person can state.
PERSON_ONLY = frozenset({"crown_jewels", "owners", "blind_spots"})

ADDED, CHANGED, REMOVED = "added", "changed", "removed"
OPEN, APPLIED, OUTDATED, DISCARDED = "open", "applied", "outdated", "discarded"


_REF = re.compile(r"r[1-9][0-9]*")


class ProposalRefused(Exception):
    """A save or discard refused, with the reason the caller is shown."""


# --- Items and the diff ------------------------------------------------------


def _groups(items: Dict[str, Any]) -> Dict[Tuple[str, str], List[str]]:
    """(list, label) -> the canonical content of every item under that label.

    Two items sharing a label (one team owning two ranges) compare as a group.
    File and line are where an item sits, not what it says.
    """
    out: Dict[Tuple[str, str], List[str]] = {}
    for kind, field in LABEL.items():
        for item in (items or {}).get(kind) or []:
            body = {k: v for k, v in item.items() if k not in ("file", "line")}
            out.setdefault((kind, str(item.get(field))), []).append(
                json.dumps(body, sort_keys=True)
            )
    return {k: sorted(v) for k, v in out.items()}


def diff(proposed: Dict[str, Any], applied: Dict[str, Any]) -> List[Dict[str, str]]:
    """Each (list, label) the proposal adds, changes or removes against ``applied``."""
    new, old = _groups(proposed), _groups(applied)
    out = []
    for key in sorted(new.keys() | old.keys()):
        if key not in old:
            change = ADDED
        elif key not in new:
            change = REMOVED
        elif new[key] != old[key]:
            change = CHANGED
        else:
            continue
        out.append({"kind": key[0], "item": key[1], "change": change})
    return out


def _applied_items(row: Dict[str, Any]) -> Dict[str, Any]:
    return (row.get("items") or empty_items()) if row.get("revision") else empty_items()


def _files(raw: Any) -> List[Tuple[str, str]]:
    """``[(site/<name>.md, text)]`` in name order, or :class:`ProposalRefused`."""
    if not isinstance(raw, list) or not raw:
        raise ProposalRefused("files must be a non-empty list of {file, text}")
    out: Dict[str, str] = {}
    for entry in raw:
        name = entry.get("file") if isinstance(entry, dict) else None
        body = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(body, str):
            raise ProposalRefused("each file is {file, text}, both strings")
        bare = name.strip().removeprefix(f"{SITE_DIR_NAME}/")
        if not bare.endswith(".md") or bare == ".md" or "/" in bare or "\\" in bare:
            raise ProposalRefused(f"file {name!r} is not site/<name>.md")
        path = f"{SITE_DIR_NAME}/{bare}"
        if path in out:
            raise ProposalRefused(f"file {path} is given twice")
        out[path] = body
    return sorted(out.items())


# --- Status --------------------------------------------------------------------


def status(value: Dict[str, Any], row: Dict[str, Any]) -> str:
    """Against the revision applied now: applied, outdated, discarded or open."""
    if value.get("discarded"):
        return DISCARDED
    if row.get("revision") and not diff(value.get("items") or {}, _applied_items(row)):
        return APPLIED
    if row.get("revision") != value.get("base_revision"):
        return OUTDATED
    return OPEN


# --- The row -----------------------------------------------------------------


def _read(session, lock: bool = False) -> Optional[Dict[str, Any]]:
    from core.storage.models import SystemConfig

    if lock:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})
    row = session.get(SystemConfig, PROPOSAL_KEY, populate_existing=lock)
    return dict(row.value) if row and row.value else None


def _write(session, value: Dict[str, Any], by: str) -> None:
    """Run state, not configuration: no config audit row, as gap_run.write_raised."""
    from core.storage.models import SystemConfig

    row = session.get(SystemConfig, PROPOSAL_KEY)
    if row is None:
        session.add(
            SystemConfig(
                key=PROPOSAL_KEY,
                value=value,
                description="The open site/ proposal (#5229)",
                config_type="site",
                updated_by=by,
            )
        )
    else:
        row.value = value
        row.updated_by = by


def _is_proposal(value: Optional[Dict[str, Any]]) -> bool:
    """A row the refresh wrote before any save holds only ``refresh``: no proposal."""
    return bool(value and value.get("id"))


def read_proposal() -> Optional[Dict[str, Any]]:
    from core.storage.connection import get_db_manager

    with get_db_manager().session_scope() as session:
        return _read(session)


def _audit(session, action: str, proposal_id: str, username, reason=None) -> None:
    """One trail entry naming the proposal and the principal; never answer text."""
    from core.audit import trail
    from core.cases import case_trail

    case_trail.append_or_legacy(
        session,
        action=action,
        signed_in=username,
        case_id=None,
        operator_text=reason,
        ids=lambda: {"proposal_id": proposal_id, trail.OBJECT: PROPOSAL_KEY},
    )


# --- Provenance ----------------------------------------------------------------


def _is_person(claims: Any) -> bool:
    from core.auth.principals import CHAT_PLUGIN_ID, agent_principal_id

    return bool(
        claims.person_id
        and claims.origin == "person"
        and claims.agent_id == agent_principal_id(CHAT_PLUGIN_ID)
    )


async def _provenance(entry: Dict[str, Any], claims: Any, now: str) -> Dict[str, Any]:
    """One given provenance checked: a call on this run's ledger, or a person's answer."""
    from core.agents.projections import read_call
    from core.llm.tool_schemas import CALL_INTEGRATION_TOOL

    ref, answer = entry.get("ref"), entry.get("answer")
    what = f"{entry.get('kind')} {entry.get('item')!r}"
    if bool(ref) == bool(answer):
        raise ProposalRefused(f"{what}: give exactly one of ref or answer")
    if answer:
        if not isinstance(answer, str) or not answer.strip():
            raise ProposalRefused(f"{what}: answer must be the person's words")
        if not _is_person(claims):
            raise ProposalRefused(
                f"{what}: a person's answer needs a chat turn a person is driving"
            )
        return {
            "source": "answer",
            "person_id": claims.person_id,
            "run_id": claims.run_id,
            "at": now,
            "answer": answer.strip(),
        }
    # The run is the caller's own, never one the model names; a ref is r<n>
    # only, so no path in it can reach another run's calls.
    ref = str(ref).strip()
    if not _REF.fullmatch(ref):
        raise ProposalRefused(f"{what}: ref {ref!r} is not a call ref (r<n>)")
    call = await read_call(claims.run_id, ref)
    if call is None or call.get("run_id") != claims.run_id:
        raise ProposalRefused(f"{what}: ref {ref} is not a call on this run")
    if call.get("ok") is not True or call.get("failure"):
        raise ProposalRefused(
            f"{what}: call {ref} failed ({call.get('failure') or 'not ok'}); "
            "a failed call is not evidence"
        )
    tool, arguments = call.get("tool"), call.get("arguments")
    if isinstance(arguments, str):  # the ledger keeps them as the model sent them
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if tool == CALL_INTEGRATION_TOOL and isinstance(arguments, dict):
        tool, arguments = arguments.get("name") or tool, arguments.get("arguments")
    cited = {
        "source": "citation",
        "run_id": claims.run_id,
        "ref": ref,
        "tool": tool,
        "arguments": arguments,
        "at": call.get("ts"),
    }
    if call.get("server"):  # only when the ledger names it
        cited["server"] = call["server"]
    return cited


# --- The save ------------------------------------------------------------------


async def save_site_proposal(
    *,
    files: Any = None,
    provenance: Any = None,
    questions: Any = None,
    registry: Any = None,
) -> Dict[str, Any]:
    """The ``save_site_proposal`` backend tool. Errors are returned, not raised."""
    import asyncio

    from core.auth.tool_principal import current_claims

    claims = current_claims()
    try:
        if claims is None or not claims.run_id:
            raise ProposalRefused("a proposal is saved from a run's tool call")
        named = _files(files)
        try:
            items = parse_site(named)
        except SiteRejected as exc:
            raise ProposalRefused(str(exc)) from None  # file:line: reason
        if questions is not None and not (
            isinstance(questions, list)
            and all(isinstance(q, str) and q.strip() for q in questions)
        ):
            raise ProposalRefused("questions must be a list of non-empty strings")
        now = utcnow().isoformat()
        given: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for entry in provenance or []:
            if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
                raise ProposalRefused(
                    f"each provenance is {{kind, item, ref | answer}}, kind one of "
                    f"{', '.join(KINDS)}"
                )
            key = (entry["kind"], str(entry.get("item")))
            if key in given:
                raise ProposalRefused(f"{key[0]} {key[1]!r}: provenance given twice")
            given[key] = await _provenance(entry, claims, now)
        try:
            registry = await site_registry(registry)
            skipped = await asyncio.to_thread(not_evaluated, registry)
        except Exception:  # noqa: BLE001 - an advisory list never costs the save
            logger.exception("site sources not evaluated: not worked out")
            skipped = []
        return await asyncio.to_thread(
            _store,
            named,
            items,
            given,
            [q.strip() for q in questions or []],
            claims,
            skipped,
        )
    except ProposalRefused as exc:
        return {"error": f"save_site_proposal refused: {exc}"}


def _store(named, items, given, questions, claims, skipped) -> Dict[str, Any]:
    """Check provenance against the diff and replace the row, under the lock."""
    from core.storage.connection import get_db_manager

    row = applied_site(fresh=True)
    # Unpacking the archive adds and overwrites; it cannot delete a file.
    dropped = sorted(
        {f.get("file") for f in row.get("files") or []} - {n for n, _ in named}
    )
    if row.get("revision") and dropped:
        raise ProposalRefused(
            f"{', '.join(dropped)} is in the applied revision: keep it, emptied to "
            "'---' and '---' if it should declare nothing"
        )
    changes = diff(items, _applied_items(row))
    if not changes:
        raise ProposalRefused(
            f"the folder matches applied revision {row.get('revision')}: nothing to propose"
        )
    with get_db_manager().session_scope() as session:
        held = _read(session, lock=True)
        kept_from = held if _is_proposal(held) and status(held, row) == OPEN else None
        theirs = _groups(kept_from.get("items") or {}) if kept_from else {}
        ours = _groups(items)
        stored = {}
        for change in changes:
            key = (change["kind"], change["item"])
            prov = given.get(key)
            if prov is None and kept_from:
                # Unchanged from the open proposal: it keeps what it rested on.
                same = theirs.get(key) == ours.get(key)
                prov = (
                    (kept_from.get("provenance") or {}).get(_pkey(*key))
                    if same
                    else None
                )
            what = f"{change['kind']} {change['item']!r} ({change['change']})"
            if prov is None:
                raise ProposalRefused(
                    f"{what} has no provenance: cite the call it rests on, or a "
                    "person's answer"
                )
            if change["kind"] in PERSON_ONLY and prov.get("source") != "answer":
                raise ProposalRefused(
                    f"{what} rests only on a person's answer, not a cited call"
                )
            stored[_pkey(*key)] = {**prov, "change": change["change"]}
        unused = sorted(
            f"{k} {i!r}"
            for k, i in given.keys() - {(c["kind"], c["item"]) for c in changes}
        )
        if unused:
            raise ProposalRefused(
                f"provenance names no item added, changed or removed: {', '.join(unused)}"
            )
        value = {
            "id": str(uuid.uuid4()),
            "saved_at": utcnow().isoformat(),
            "saved_by": {
                "person_id": claims.person_id,
                "username": claims.username,
                "agent_id": claims.agent_id,
                "run_id": claims.run_id,
                "origin": claims.origin,
            },
            "base_revision": row.get("revision"),
            "files": [{"file": n, "text": t} for n, t in named],
            "items": items,
            "provenance": stored,
            "questions": questions,
            # What the last refresh saw (start_refresh); a save keeps it.
            "refresh": (held or {}).get("refresh"),
            "not_evaluated": skipped,
            "discarded": None,
        }
        _write(session, value, claims.person_id or claims.agent_id or "agent")
        _audit(session, SAVE_ACTION, value["id"], claims.username)
    return {
        "saved": value["id"],
        "base_revision": value["base_revision"],
        "diff": changes,
        "status": OPEN,
    }


def _pkey(kind: str, item: str) -> str:
    return f"{kind}/{item}"


# --- Discard and download --------------------------------------------------------


def discard(session, reason: str, username: Optional[str]) -> Dict[str, Any]:
    """Mark the open proposal discarded with ``reason``; the reason rides the audit."""
    reason = (reason or "").strip()
    if not reason:
        raise ProposalRefused("a discard needs a reason")
    held = _read(session, lock=True)
    if not _is_proposal(held):
        raise LookupError("there is no proposal")
    state = status(held, applied_site(fresh=True))
    if state in (DISCARDED, APPLIED):
        raise ProposalRefused(f"the proposal is already {state}")
    held["discarded"] = {"at": utcnow().isoformat(), "by": username, "reason": reason}
    _write(session, held, username or "unknown")
    _audit(session, DISCARD_ACTION, held["id"], username, reason)
    return held


def archive(value: Dict[str, Any]) -> bytes:
    """The proposed folder as a .tar.gz of ``site/*.md``, to unpack into the declared directory."""
    out = io.BytesIO()
    try:
        saved = datetime.fromisoformat(value["saved_at"])  # naive UTC
        mtime = saved.replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError):
        mtime = 0
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for entry in value.get("files") or []:
            data = entry["text"].encode("utf-8")
            info = tarfile.TarInfo(entry["file"])
            info.size, info.mode, info.mtime = len(data), 0o644, mtime
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


# --- The read --------------------------------------------------------------------


def proposal_view(row: Dict[str, Any], session=None) -> Optional[Dict[str, Any]]:
    """The proposal with its status and its diff against the revision applied now."""
    value = _read(session) if session is not None else read_proposal()
    if not _is_proposal(value):
        return None
    state = status(value, row)
    return {
        **{k: v for k, v in value.items() if k != "items"},
        "status": state,
        "diff": (
            []
            if state == APPLIED
            else diff(value.get("items") or {}, _applied_items(row))
        ),
    }


def link_answers(proposal: Optional[Dict[str, Any]], user_id: Optional[str]) -> None:
    """Name the person behind each quoted answer and, when the reader owns the
    conversation it was given in, that conversation's id. Nothing is stored."""
    answers = [
        p
        for p in ((proposal or {}).get("provenance") or {}).values()
        if p.get("source") == "answer"
    ]
    if not answers:
        return
    from core.chat.conversation_service import conversations_for_runs
    from core.storage.connection import get_db_manager
    from core.storage.models import User

    owned = conversations_for_runs([p.get("run_id") for p in answers], user_id)
    ids = {p["person_id"] for p in answers if p.get("person_id")}
    with get_db_manager().session_scope() as session:
        names = dict(
            session.query(User.user_id, User.username).filter(User.user_id.in_(ids))
        )
    for p in answers:
        p["person"] = names.get(p.get("person_id"))
        p["conversation_id"] = owned.get(p.get("run_id"))


def site_questions(view: Dict[str, Any], row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Unplaced, unowned and contradicted, against the applied ``row`` and its ``view``."""
    from core.memory.recall import _OFFERED
    from core.storage.connection import get_db_manager

    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    # A restricted case's memory is not offered here, as recall offers it to no
    # read outside that case (#4358).
    offered = _OFFERED.format(alias="episodic_sightings")
    with get_db_manager().session_scope() as session:
        touched = session.execute(
            text("""
                SELECT entity_key, count(DISTINCT investigation_id) AS n,
                       max(concluded_at) AS latest
                FROM episodic_sightings
                WHERE concluded_at >= :since
                  AND (entity_key LIKE 'host:%' OR entity_key LIKE 'ip:%')
                  AND {offered}
                GROUP BY entity_key
                HAVING count(DISTINCT investigation_id) >= :floor
                ORDER BY n DESC, latest DESC, entity_key
            """.format(offered=offered)),
            {"since": since, "floor": QUESTION_FLOOR, "own_case": None},
        ).all()
        stated = (
            session.execute(
                text("""
                SELECT DISTINCT entity_key FROM entity_facts
                WHERE expires_at > :now AND entity_key LIKE 'host:%'
                  AND case_id IS NULL
                  AND lower(facts->>'criticality') = 'critical'
                ORDER BY entity_key
            """),
                {"now": utcnow()},
            )
            .scalars()
            .all()
        )
        counts = {}
        if stated:
            counts = {
                k: (n, latest)
                for k, n, latest in session.execute(
                    text("""
                        SELECT entity_key, count(DISTINCT investigation_id),
                               max(concluded_at)
                        FROM episodic_sightings
                        WHERE concluded_at >= :since AND entity_key = ANY(:keys)
                          AND {offered}
                        GROUP BY entity_key
                    """.format(offered=offered)),
                    {"since": since, "keys": list(stated), "own_case": None},
                ).all()
            }

    def line(question: str, key: str, n: Any, latest: Any) -> Dict[str, Any]:
        return {
            "question": question,
            "kind": key.partition(":")[0],
            "entity_key": key,
            "investigations": n or 0,
            "latest": latest.isoformat() if latest else None,
        }

    out: List[Dict[str, Any]] = []
    unplaced = [
        line("unplaced", key, n, latest)
        for key, n, latest in touched
        if not any(m["kind"] == "zones" for m in match([key], row))
    ]
    out += unplaced[:MAX_QUESTIONS]
    for kind, label in (("zones", "zone"), ("crown_jewels", "crown_jewel")):
        for item in view.get(kind) or []:
            if not any(o.get("resolved") for o in item.get("owners") or []):
                out.append(
                    {
                        "question": "unowned",
                        "kind": label,
                        "name": item.get("name"),
                        "file": item.get("file"),
                        "line": item.get("line"),
                    }
                )
    contradicted = [
        line("contradicted", key, *counts.get(key, (0, None)))
        for key in stated
        if not any(m["kind"] == "crown_jewels" for m in match([key], row))
    ]
    out += contradicted[:MAX_QUESTIONS]
    return out


def archive_name(value: Dict[str, Any]) -> str:
    return f"site-proposal-{str(value.get('id'))[:8]}.tar.gz"


# --- Sources not evaluated (#5259) -----------------------------------------------

SITE_AGENT = "site_context"
SITE_WORKFLOW = f"agent-run-{SITE_AGENT}"
WHERE_CLASS = "Settings → Limits & autonomy"


def _site_needs() -> List[str]:
    import yaml

    from core.agents.builtins import builtin_manifests

    return list(yaml.safe_load(builtin_manifests()[SITE_AGENT]).get("needs") or [])


def _need_servers(needs: List[str]) -> List[str]:
    """Every outside server a need may bind: the servers a capability's bound
    tools name, or the ``{server}`` of a catalogue name."""
    from core.integrations.bindings import servers_for
    from core.response.ceiling import own_tool_names
    from core.workflows.playbook_resolver import CAPABILITY_NAMES, NEED_ALIASES

    own = own_tool_names()
    servers: Dict[str, None] = {}
    for need in needs:
        capability = NEED_ALIASES.get(need, need)
        if need in own:
            continue
        if capability in CAPABILITY_NAMES:
            servers.update(dict.fromkeys(servers_for(capability)))
        elif "_" in need:
            servers[need.partition("_")[0]] = None
    return list(servers)


_DIAL_TIMEOUT_S = 10


async def site_registry(registry: Any = None) -> Any:
    """The registry the site agent's needs bind against.

    The API passes its own, which a run binds against. Elsewhere (the daemon's
    tick) this process dials the servers those needs name, as containment
    dials its vendors; ``connect_to_server`` skips one disabled or without
    credentials.
    """
    import asyncio

    from core.integrations.mcp.client import process_mcp_client
    from core.integrations.mcp.registry import MCPRegistry

    if registry is not None:
        return registry
    client = process_mcp_client()
    if client is not None:
        connected = getattr(client, "tools_cache", None) or {}
        for server in _need_servers(_site_needs()):
            if server in connected:
                continue
            try:
                await asyncio.wait_for(
                    client.connect_to_server(server, persistent=True), _DIAL_TIMEOUT_S
                )
            except Exception:  # noqa: BLE001 - it reads as not connected
                logger.debug("site agent server %s not connected", server)
    return MCPRegistry()


def not_evaluated(registry=None) -> List[Dict[str, str]]:
    """The site agent's outside tools that cannot run on this deployment.

    From its manifest's needs and what each binds here, a capability's tools
    included: a server not connected, or a tool not classed read or
    read_unverified. Vigil's own tools are never listed.
    """
    from core.integrations.mcp.registry import MCPRegistry
    from core.response.ceiling import is_read, own_tool_names, tool_classes
    from core.workflows.playbook_resolver import (
        CAPABILITY_NAMES,
        NEED_ALIASES,
        _bound_capabilities,
        _tool_catalogue,
    )

    needs = _site_needs()
    bound = _bound_capabilities(needs, _tool_catalogue(registry or MCPRegistry()))
    provided = {b["provides"] for b in bound}
    own = own_tool_names()
    out: List[Dict[str, str]] = []
    for need in needs:
        capability = NEED_ALIASES.get(need, need)
        if capability in provided or need in own:
            continue
        if capability in CAPABILITY_NAMES:
            reason = f"not connected: no connected source answers {capability}"
        else:  # a catalogue name, {server}_{tool}
            reason = f"not connected: {need.partition('_')[0]} is not connected"
        out.append({"tool": need, "reason": reason})
    for name, effect in tool_classes(b["id"] for b in bound).items():
        if not is_read(effect):
            out.append(
                {
                    "tool": name,
                    "reason": (
                        f"classed {effect}, not read" if effect else "not classed"
                    ),
                    "where": WHERE_CLASS,
                }
            )
    return out


def _skipped_line(line: Dict[str, str]) -> str:
    where = f"; set its class in {line['where']}" if line.get("where") else ""
    return f"{line['tool']} ({line['reason']}{where})"


# --- The refresh (#5259) ---------------------------------------------------------

QUEUED, REFUSED, NOTHING_NEW, NOT_QUEUED = (
    "queued",
    "refused",
    "nothing_new",
    "not_queued",
)
_REFRESH_LOCK = 0x7669_6769_6C53_5246  # "vigilSRF"

AGENT_OFF = (
    f"The site refresh is turned off: the site context agent or its workflow "
    f"{SITE_WORKFLOW} is off. Turn it on in Workflows → Agents."
)
AUTOOPS_OFF = (
    "AutoOps is off, so a site refresh would not launch. Turn AutoOps on, then "
    "start the refresh again."
)

_OPENING = (
    "Refresh the site/ proposal. No person is present. Read the applied site, "
    "the open proposal and the site questions with get_site_context, draft from "
    "the sources you hold, and save the full folder with save_site_proposal: "
    "every file, each changed item citing the call it rests on. Propose "
    "selector changes (widen a zone, or add one), never one host as an "
    "instance. Draft no crown jewel, owner or blind spot: put each thing only a "
    "person can settle in questions, and answer none of them."
)


def _outcome(state: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"status": state, "message": message, **extra}


def _autoops_on() -> bool:
    """The ``orchestrator.settings`` row's ``enabled``, as the orchestrator reads it;
    with no row, ``ORCHESTRATOR_ENABLED``, which the orchestrator starts from."""
    from core.config import get_settings
    from core.storage.connection import get_db_manager
    from core.storage.models import SystemConfig

    with get_db_manager().session_scope() as session:
        cfg = session.get(SystemConfig, "orchestrator.settings")
        if cfg and isinstance(cfg.value, dict):
            return bool(cfg.value.get("enabled", False))
    return bool(get_settings().orchestrator_enabled)


def _unfinished() -> Optional[Tuple[int, Optional[str]]]:
    """(intake id, investigation id) of a site refresh still queued or working."""
    from sqlalchemy import and_, or_, select

    from core.detections.engineering.proposals import _RUNNING_INVESTIGATION_STATUSES
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger, Investigation

    with get_db_manager().session_scope() as session:
        row = session.execute(
            select(IntakeTrigger.id, IntakeTrigger.investigation_id)
            .outerjoin(
                Investigation,
                Investigation.investigation_id == IntakeTrigger.investigation_id,
            )
            .where(
                IntakeTrigger.kind == "schedule",
                IntakeTrigger.payload["workflow_id"].astext == SITE_WORKFLOW,
                or_(
                    IntakeTrigger.state == "queued",
                    and_(
                        IntakeTrigger.state == "launched",
                        Investigation.status.in_(_RUNNING_INVESTIGATION_STATUSES),
                    ),
                ),
            )
            .order_by(IntakeTrigger.id)
        ).first()
    return None if row is None else (row[0], row[1])


class _Lock:
    """Session advisory lock for one start; ``got`` is False when another holds it."""

    def __enter__(self) -> bool:
        from core.storage.connection import get_db_manager

        self.conn = get_db_manager().engine.connect()
        self.got = False
        try:
            self.got = bool(
                self.conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": _REFRESH_LOCK}
                ).scalar()
            )
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise
        return self.got

    def __exit__(self, *exc) -> None:
        try:
            if self.got:
                self.conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _REFRESH_LOCK}
                )
                self.conn.commit()
        finally:
            self.conn.close()


def _question_key(q: Dict[str, Any]) -> str:
    return f"{q.get('question')} " + (
        q.get("entity_key") or f"{q.get('kind')} {q.get('name')}"
    )


def _entries(view: Dict[str, Any]) -> Dict[str, List[str]]:
    """What a refresh sees: the report's check outcomes and item states (declared
    names, never the hosts behind them) and the site-question keys."""
    report = view.get("report") or {}
    checks = {
        f"check {c.get('id')}: {c.get('outcome')}" for c in report.get("checks") or []
    }
    items = {
        f"{i.get('kind')} {i.get('name')}: {i.get('state')}"
        for i in (report.get("evidence") or {}).get("items") or []
    }
    return {
        "report": sorted(checks | items),
        "questions": sorted(
            {_question_key(q) for q in view.get("site_questions") or []}
        ),
    }


def _opening(
    proposal: Optional[Dict[str, Any]],
    new: Dict[str, List[str]],
    skipped: List[Dict[str, str]],
) -> str:
    parts = [_OPENING]
    if proposal:
        parts.append(
            f"The proposal saved {proposal.get('saved_at')} is {proposal['status']}"
            + (
                ": start from its files, and keep every crown jewel, owner and "
                "blind spot in it exactly as it is."
                if proposal["status"] == OPEN
                else "."
            )
        )
    else:
        parts.append("There is no proposal: start from the applied revision.")
    if new["questions"]:
        parts.append(
            "Site questions new since the last refresh: "
            + "; ".join(new["questions"])
            + "."
        )
    if new["report"]:
        parts.append(
            "Day 1 Report site entries new since the last refresh: "
            + "; ".join(new["report"])
            + "."
        )
    if skipped:
        parts.append(
            "Sources not evaluated; do not call these tools, they cannot run here: "
            + "; ".join(_skipped_line(s) for s in skipped)
            + "."
        )
    return " ".join(parts)


async def start_refresh(
    person: Optional[Dict[str, str]] = None, registry: Any = None
) -> Dict[str, Any]:
    """Queue the site agent with no Case; the console route's and the daily tick's start.

    ``person`` is ``started_by`` and ``principal`` of whoever started it; None is
    the tick, which queues only when a revision is applied, the proposal is not
    open, and the report or the site questions hold an entry the last refresh
    did not see. What it saw is written once the row is queued, in the same
    transaction, so a discard does not re-arm the tick.
    """
    import asyncio

    from core.agents.enablement import disabled_agent_ids
    from core.workflows.enablement import is_enabled

    if not is_enabled(SITE_WORKFLOW) or SITE_AGENT in disabled_agent_ids():
        return _outcome(REFUSED, AGENT_OFF)
    if not await asyncio.to_thread(_autoops_on):
        return _outcome(REFUSED, AUTOOPS_OFF)
    registry = await site_registry(registry)
    return await asyncio.to_thread(_start_refresh, person, registry)


def _start_refresh(person: Optional[Dict[str, str]], registry: Any) -> Dict[str, Any]:
    from core.site import site_view
    from core.storage.connection import get_db_manager
    from core.storage.intake_queue import insert_intake_trigger

    with _Lock() as got:
        if not got:
            return _outcome(REFUSED, "Another site refresh is being started right now.")
        unfinished = _unfinished()
        if unfinished is not None:
            intake_id, investigation_id = unfinished
            which = (
                f"investigation {investigation_id}"
                if investigation_id
                else "still queued"
            )
            return _outcome(
                REFUSED,
                f"A site refresh is unfinished (intake row {intake_id}, {which}); "
                "start another once it ends.",
                intake_id=intake_id,
            )
        row = applied_site(fresh=True)
        if person is None and not row.get("revision"):
            return _outcome(NOTHING_NEW, "No site/ revision is applied.")
        view = site_view(row)
        proposal = view.get("proposal")
        if person is None and proposal and proposal["status"] == OPEN:
            return _outcome(
                NOTHING_NEW, "The site proposal is open; a person reviews it first."
            )
        now = _entries(view)
        before = (read_proposal() or {}).get("refresh") or {}
        new = {
            k: [e for e in v if e not in set(before.get(k) or [])]
            for k, v in now.items()
        }
        if person is None and not any(new.values()):
            return _outcome(NOTHING_NEW, "Nothing new since the last site refresh.")
        skipped = not_evaluated(registry)
        hypothesis = _opening(proposal, new, skipped)
        with get_db_manager().session_scope() as session:
            queued = insert_intake_trigger(
                kind="schedule",
                priority="low",
                payload={
                    "workflow_id": SITE_WORKFLOW,
                    "trigger_type": "manual" if person else "scheduled",
                    "finding_ids": [],
                    "hypothesis": hypothesis,
                    "no_case": True,
                    **(person or {}),
                },
                session=session,
            )
            if queued is None:
                return _outcome(
                    NOT_QUEUED, "The site refresh's intake row was not queued."
                )
            held = _read(session, lock=True) or {}
            held["refresh"] = {"at": utcnow().isoformat(), "intake_id": queued, **now}
            _write(session, held, (person or {}).get("started_by") or "system")
            if person:
                _audit(
                    session, REFRESH_ACTION, held.get("id"), person.get("started_by")
                )
            else:
                from core.audit import trail
                from core.auth import trail_events

                trail_events.record(
                    session,
                    REFRESH_ACTION,
                    actor="system",
                    proposal_id=held.get("id"),
                    **{trail.OBJECT: PROPOSAL_KEY},
                )
    return _outcome(QUEUED, hypothesis, intake_id=queued, not_evaluated=skipped)
