"""Entity facts: what Entra, Okta and Falcon say about a finding's users and hosts.

The single home of the concept (#4741, epic #4719). ``read`` serves unexpired
rows of ``entity_facts`` and fetches the misses from the connected sources
through ``execute_mcp_tool``, inside the limits below; nothing preloads or
prefetches. Each source has one projection, and the projection is the
allowlist: identity, title, groups, manager, device ownership, account state,
and criticality only where the source states it. Nothing else is stored, so
credentials, auth factors and HR data cannot be.

Facts inform and nothing branches on them: they go into the triage prompt and
its stored result, never into a gate, ceiling or verdict.

Triage's rows carry no Case (``case_id`` NULL). A run on a Case reads through
``read_keys`` (#4742): it serves that Case's own rows, copies a fresh caseless
row onto the Case before asking a source, and stores what it fetched on the
Case; only a principal that may read the Case reads or writes its rows. The sweep in
``services/daemon/scheduler.py`` calls ``sweep`` to delete expired rows and rows
whose Case closed. One trail entry per read and one per sweep, never per row.

Each read also carries "Declared" rows (#5211): what the applied site revision
(``core/site.py``) declares about every key it names, ``ip:`` keys included. They
come from no source, are never stored or counted, and a key joins the read for
them only when a declared item matched it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from sqlalchemy import text

from core.time import utcnow

logger = logging.getLogger(__name__)

LIMITS_KEY = "entity_facts.limits"
ACTION_READ = "entity_facts.read"
ACTION_SWEEP = "entity_facts.sweep"


@dataclass(frozen=True)
class FactLimits:
    max_keys: int = 10  # entities per read
    calls_per_minute: int = 30  # tool calls per source
    budget_seconds: int = 5  # the whole read, connecting included
    max_rows: int = 10_000  # rows held; the oldest go first at the cap
    ttl_hours: int = 24
    failure_skip_seconds: int = 300  # a source that failed is not asked again


DEFAULTS = FactLimits()
BOUNDS: Dict[str, Tuple[int, int]] = {
    "max_keys": (1, 100),
    "calls_per_minute": (1, 1000),
    "budget_seconds": (1, 60),
    "max_rows": (100, 1_000_000),
    "ttl_hours": (1, 24 * 30),
    "failure_skip_seconds": (0, 3600),
}


def _parse(value: Any) -> FactLimits:
    """The stored value; each field that is not valid reads as its default."""
    if not isinstance(value, dict):
        if value is not None:
            logger.error("%s is not an object; defaults apply", LIMITS_KEY)
        return DEFAULTS
    fields: Dict[str, int] = {}
    for name, (low, high) in BOUNDS.items():
        if name not in value:
            continue
        v = value[name]
        if isinstance(v, int) and not isinstance(v, bool) and low <= v <= high:
            fields[name] = v
        else:
            logger.error(
                "%s.%s=%r is not a whole number from %d to %d; the default applies",
                LIMITS_KEY,
                name,
                v,
                low,
                high,
            )
    return replace(DEFAULTS, **fields)


def read_limits() -> FactLimits:
    """The limits in force, read on each use. Never raises: a failed read is the defaults."""
    from core.storage import config_service

    try:
        return _parse(
            config_service.get_config_service().read_system_config(LIMITS_KEY)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("%s not read; defaults apply: %s", LIMITS_KEY, exc)
        return DEFAULTS


# ---- Projections: the allowlist, one per source ----


def _first(row: Any) -> Dict[str, Any]:
    return row if isinstance(row, dict) else {}


def _names(items: Any, *path: str) -> List[str]:
    """The display names in a list of records, following ``path`` into each."""
    out = []
    for item in items if isinstance(items, list) else []:
        for step in path:
            item = item.get(step) if isinstance(item, dict) else None
        if isinstance(item, str) and item:
            out.append(item)
    return out


def _entra(rows: List[Any]) -> Dict[str, Any]:
    row = _first(rows[0] if rows else None)
    if row.get("found") is False:
        return {"found": False}
    manager = _first(row.get("manager"))
    return {
        "display name": row.get("displayName"),
        "user principal name": row.get("userPrincipalName") or row.get("mail"),
        "title": row.get("jobTitle"),
        "user type": row.get("userType"),
        "account": {True: "enabled", False: "disabled"}.get(row.get("accountEnabled")),
        "manager": manager.get("displayName") or manager.get("userPrincipalName"),
        "groups": [g for g in row.get("groups") or [] if isinstance(g, str)],
    }


def _okta_user(rows: List[Any]) -> Dict[str, Any]:
    row = _first(rows[0] if rows else None)
    profile = _first(row.get("profile"))
    name = profile.get("displayName") or " ".join(
        p for p in (profile.get("firstName"), profile.get("lastName")) if p
    )
    return {
        "display name": name or None,
        "login": profile.get("login"),
        "title": profile.get("title"),
        "manager": profile.get("manager"),
        "status": row.get("status"),
    }


def _okta_groups(rows: List[Any]) -> Dict[str, Any]:
    envelope = _first(rows[0] if rows else None)
    groups = envelope["groups"] if "groups" in envelope else rows
    return {"groups": _names(groups, "profile", "name")}


def _falcon_host(rows: List[Any]) -> Dict[str, Any]:
    envelope = _first(rows[0] if rows else None)
    hosts = envelope.get("results") if "results" in envelope else rows
    host = _first(hosts[0] if isinstance(hosts, list) and hosts else None)
    if not host:
        return {"found": False}
    return {
        "hostname": host.get("hostname"),
        "platform": host.get("platform_name"),
        "os": host.get("os_version"),
        "containment": host.get("status"),
        "last user": host.get("last_login_user"),
        "tags": [t for t in host.get("tags") or [] if isinstance(t, str)],
        "host group ids": [g for g in host.get("groups") or [] if isinstance(g, str)],
        "last seen": host.get("last_seen"),
    }


@dataclass(frozen=True)
class Call:
    """One tool call of a source: the tool's names in preference order, matched
    against what the server reports (as ``Candidate`` does in
    ``core/workflows/playbook_resolver.py``), its arguments, and its projection.
    ``required`` False: a missing tool or a failed call only leaves its fields out."""

    tools: Tuple[str, ...]
    args: Callable[[str], Dict[str, Any]]
    project: Callable[[List[Any]], Dict[str, Any]]
    required: bool = True


@dataclass(frozen=True)
class Source:
    server: str  # as mcp-config.json names it
    label: str  # how the prompt names it
    kinds: Tuple[str, ...]  # Entity Key types it answers
    calls: Tuple[Call, ...]


def _fql(value: str) -> str:
    return "hostname:'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


SOURCES: Tuple[Source, ...] = (
    # In-repo: core/integrations/azure_ad/tool.py.
    Source(
        "azure-ad",
        "Entra",
        ("user", "email"),
        (Call(("aad_get_user",), lambda v: {"user": v}, _entra),),
    ),
    # fctr-id/okta-mcp-server and falcon-mcp are third-party: preferences.
    Source(
        "okta",
        "Okta",
        ("user", "email"),
        (
            Call(("get_okta_user",), lambda v: {"user_id": v}, _okta_user),
            Call(
                ("list_okta_user_groups",),
                lambda v: {"user_id": v},
                _okta_groups,
                required=False,
            ),
        ),
    ),
    Source(
        "crowdstrike",
        "Falcon",
        ("host",),
        (
            Call(
                ("falcon_search_hosts",),
                lambda v: {"filter": _fql(v), "limit": 1},
                _falcon_host,
            ),
        ),
    ),
)
_BY_SERVER = {s.server: s for s in SOURCES}
KINDS = {kind for s in SOURCES for kind in s.kinds}


def _clean(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Drop what a source left empty; the allowlist decides the rest."""
    return {k: v for k, v in facts.items() if v not in (None, "", [], {})}


def _not_found(facts: Dict[str, Any]) -> bool:
    return facts.get("found") is False


# ---- Process state: per-source failure skip and call rate (daemon memory) ----

_failed: Dict[str, Tuple[float, str]] = {}  # server -> (skip until, reason)
_calls: Dict[str, Deque[float]] = {}
# Entra and Okta have no separate credential check that a fact read would
# duplicate. A successful read is the real check, written at most once a minute.
_CHECK_ON_READ = frozenset({"azure-ad", "okta"})
_check_marked: Dict[str, float] = {}


def _mark_check(server: str) -> None:
    if server not in _CHECK_ON_READ:
        return
    now = time.monotonic()
    if now - _check_marked.get(server, 0.0) < 60:
        return
    _check_marked[server] = now
    from core.storage.source_health import record_test

    record_test(server, success=True, error=None, check=(True, "Read entity facts"))


def _skipped(server: str) -> Optional[str]:
    until, reason = _failed.get(server, (0.0, ""))
    left = until - time.monotonic()
    return (
        f"skipped for {int(left) + 1}s more after it failed: {reason}"
        if left > 0
        else None
    )


def _fail(server: str, reason: str, limits: FactLimits) -> None:
    _failed[server] = (time.monotonic() + limits.failure_skip_seconds, reason)


def _take_call(server: str, limits: FactLimits) -> bool:
    """Count one call against the per-minute cap; False when it is spent."""
    now = time.monotonic()
    window = _calls.setdefault(server, deque())
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= limits.calls_per_minute:
        return False
    window.append(now)
    return True


# ---- The read ----


@dataclass
class Fact:
    key: str
    value: str  # the entity as the finding spelled it
    source: str
    label: str
    facts: Dict[str, Any]
    fetched_at: datetime  # a Declared row: when its revision was applied

    @property
    def declared(self) -> bool:
        return self.label == DECLARED

    def age(self, now: datetime) -> str:
        minutes = max(0, int((now - self.fetched_at).total_seconds() // 60))
        if minutes < 60:
            return f"{minutes}m"
        return f"{minutes // 60}h{minutes % 60:02d}m"


@dataclass
class FactRead:
    facts: List[Fact] = field(default_factory=list)
    unavailable: Dict[str, str] = field(default_factory=dict)  # label -> reason
    counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    keys: List[str] = field(default_factory=list)
    dropped: int = 0  # keys past max_keys

    def fields(self) -> Dict[str, str]:
        """Each fact field by the name a KNOWN line cites, e.g. "Entra: alice@x title".
        The value leads with the entity, so a fact naming it checks out."""
        out: Dict[str, str] = {}
        for fact in self.facts:
            for name, value in fact.facts.items():
                shown = ", ".join(map(str, value)) if isinstance(value, list) else value
                cited = field_name(fact.label, fact.value, name)
                # Two Declared items on one entity share field names: keep both.
                held = out.get(cited) if fact.declared else None
                out[cited] = f"{held}; {shown}" if held else f"{fact.value}: {shown}"
        return out

    def journaled(self) -> Dict[str, Any]:
        """The read as a run journals it (#4742): rows as read, no ages, so a
        replay renders the same bytes. ``fetched_at`` is UTC."""
        return {
            "keys": list(self.keys),
            "facts": [
                {
                    "entity": f.key,
                    "value": f.value,
                    "source": f.label,
                    "fetched_at": f.fetched_at.isoformat() + "Z",
                    "fields": f.facts,
                }
                for f in self.facts
            ],
            "sources_unavailable": dict(self.unavailable),
            "dropped_keys": self.dropped,
        }

    def stored(self, now: datetime) -> Dict[str, Any]:
        """What triage was shown, kept on its result."""
        return {
            "facts": [
                {
                    "entity": f.key,
                    "source": f.label,
                    "fetched_at": f.fetched_at.isoformat(),
                    **({} if f.declared else {"age": f.age(now)}),
                    "fields": f.facts,
                }
                for f in self.facts
            ],
            "unavailable": dict(self.unavailable),
            "dropped_keys": self.dropped,
        }


def field_name(label: str, value: str, name: str) -> str:
    from core.llm.security import alert_text

    # A citation is split on "," and closed by "]": neither may be in a name.
    value = alert_text(value)
    for ch in "[],;":
        value = value.replace(ch, " ")
    return f"{label}: {' '.join(value.split())} {name}"


def candidates(
    finding: Dict[str, Any], kinds: Optional[set] = KINDS
) -> List[Tuple[str, str]]:
    """(Entity Key, value as spelled) for the finding's entities of ``kinds``
    (None: every kind), in order."""
    from core.memory.entity_keys import entity_context_candidates, entity_key

    out, seen = [], set()
    for kind, value in entity_context_candidates(finding):
        key = entity_key(kind, value)
        if (kinds is None or kind in kinds) and key and key not in seen:
            seen.add(key)
            out.append((key, value.strip()))
    return out


Row = Tuple[Dict, datetime, datetime]  # facts, fetched_at, expires_at


def _load(
    pairs: List[Tuple[str, str]], now: datetime, case_id: Optional[str] = None
) -> Dict[Tuple[str, str], Row]:
    """Unexpired rows of ``case_id`` (None: triage's) for (key, source), counting
    each served row's hit."""
    if not pairs:
        return {}
    from core.storage.unit_of_work import unit_of_work

    keys = sorted({k for k, _ in pairs})
    with unit_of_work() as session:
        rows = session.execute(
            text("""
                UPDATE public.entity_facts SET hit_count = hit_count + 1
                WHERE COALESCE(case_id, '') = :case AND expires_at > :now
                  AND entity_key = ANY(:keys)
                RETURNING entity_key, source, facts, fetched_at, expires_at
            """),
            {"now": now, "keys": keys, "case": case_id or ""},
        ).all()
    wanted = set(pairs)
    return {(k, s): (f, at, ex) for k, s, f, at, ex in rows if (k, s) in wanted}


StoredRow = Tuple[str, str, Dict, datetime, Optional[datetime]]


def _store(
    rows: List[StoredRow], limits: FactLimits, case_id: Optional[str] = None
) -> int:
    """Upsert the rows on ``case_id`` (None: triage's), then evict the oldest past
    the cap. A row with no expiry expires a TTL after it was fetched. Evicted count."""
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        for key, source, facts, at, expires in rows:
            session.execute(
                text("""
                    INSERT INTO public.entity_facts
                        (entity_key, source, case_id, facts, fetched_at, expires_at)
                    VALUES (:key, :source, :case, CAST(:facts AS jsonb), :at, :expires)
                    ON CONFLICT (entity_key, source, COALESCE(case_id, ''))
                    DO UPDATE SET facts = EXCLUDED.facts, fetched_at = EXCLUDED.fetched_at,
                        expires_at = EXCLUDED.expires_at, hit_count = 0
                """),
                {
                    "key": key,
                    "source": source,
                    "case": case_id,
                    "facts": json.dumps(facts, default=str),
                    "at": at,
                    "expires": expires or at + timedelta(hours=limits.ttl_hours),
                },
            )
    # Its own transaction: an eviction that fails must not undo the upserts.
    with unit_of_work() as session:
        return (
            session.execute(
                text("""
                DELETE FROM public.entity_facts WHERE id IN (
                    SELECT id FROM public.entity_facts
                    ORDER BY fetched_at DESC, id DESC OFFSET :cap)
            """),
                {"cap": limits.max_rows},
            ).rowcount
            or 0
        )


def _audit(
    action: str, payload: Dict[str, Any], keys: Optional[List[str]] = None
) -> None:
    """One trail entry. Keys name people, so they sit in the erasable ``personal``
    column, not the hashed payload."""
    from core.auth import trail_events

    try:
        trail_events.safety(
            action,
            beside={"personal": {"entity_keys": keys}} if keys else None,
            object="entity_facts",
            **payload,
        )
    except Exception:  # noqa: BLE001 - the read stands without its entry
        logger.exception("%s not recorded on the audit trail", action)


# A reason is (kind, text): the kind goes on the trail, whose payload is hashed and
# never erased; the text, which can quote a source's error, goes only to the prompt.
Reason = Tuple[str, str]
_registry: Optional[Any] = None
_connecting: Dict[str, "asyncio.Task[bool]"] = {}
_background: set = set()  # tasks kept alive until they finish


def _spawn(coro: Any) -> "asyncio.Task[Any]":
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _connect(source: Source, deadline: float) -> Tuple[Optional[Any], Reason]:
    """(registry, ("", "")) once the source is connected, else (None, reason)."""
    global _registry
    from core.integrations.mcp.client import process_mcp_client
    from core.integrations.mcp.registry import MCPRegistry, refresh_from_client

    client = process_mcp_client()
    if client is None:
        return None, ("no_client", "no MCP client in this process")
    if source.server not in client.mcp_service.servers:
        return None, ("not_configured", "not in mcp-config.json")
    if not client.mcp_service.is_server_enabled(source.server):
        return None, ("not_enabled", "not enabled")
    # Shielded: a cold start that outlasts this read finishes for the next one
    # rather than being torn down half-spawned.
    task = _connecting.get(source.server)
    if task is None or task.done():
        task = _spawn(client.connect_to_server(source.server))
        _connecting[source.server] = task
    try:
        on = await asyncio.wait_for(asyncio.shield(task), timeout=_left(deadline))
    except asyncio.TimeoutError:
        return None, ("timeout", "still connecting when the read's budget ran out")
    if not on:
        return None, (
            "connect_failed",
            client.last_errors.get(source.server) or "not connected",
        )
    if _registry is None:
        _registry = MCPRegistry()
    refresh_from_client(_registry)
    return _registry, ("", "")


def _refused(detail: str) -> bool:
    """A source turning down this one entity (a malformed name), not failing."""
    lowered = detail.lower()
    return bool(re.search(r"\b(400|422)\b", lowered)) or "bad request" in lowered


async def _fetch_one(
    source: Source,
    tools: List[Optional[str]],
    registry: Any,
    value: str,
    limits: FactLimits,
    deadline: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[Reason], bool]:
    """(facts, reason, source failed) for one entity."""
    from core.agents.mcp_tools import MCPFailure, execute_mcp_tool, row_error

    facts: Dict[str, Any] = {}
    read = False
    for tool, call in zip(tools, source.calls):
        if tool is None or _not_found(facts):
            continue
        if not _take_call(source.server, limits):
            return (
                None,
                ("rate_limited", f"over {limits.calls_per_minute} calls a minute"),
                False,
            )
        try:
            rows, handled = await execute_mcp_tool(
                tool, call.args(value), max(0.1, _left(deadline)), registry
            )
            error = row_error(rows)
            if error:
                raise MCPFailure("backend_error", error[:200])
        except MCPFailure as exc:
            if "not found" in exc.detail.lower() or "404" in exc.detail:
                # A missing entity, not a credential that worked. A token
                # request that failed with HTTP 404 lands here too, so this
                # branch does not mark the integration healthy.
                facts = {"found": False}
                continue
            if not call.required:
                continue
            if _refused(exc.detail):
                return None, ("entity_refused", exc.detail[:200]), False
            return None, (exc.kind, exc.detail[:200]), True
        facts.update(call.project(rows or []))
        if handled:
            read = True
    if read:
        _mark_check(source.server)
    return (facts if _not_found(facts) else _clean(facts)), None, False


async def _fetch(
    source: Source,
    items: List[Tuple[str, str]],
    limits: FactLimits,
    deadline: float,
    out: Dict[Tuple[str, str], Dict[str, Any]],
    missed: Dict[Tuple[str, str], Reason],
) -> Optional[Reason]:
    """Fetch ``items`` from ``source`` into ``out``, each entity at once. Returns
    why the source as a whole could not answer; an entity it refused alone goes
    into ``missed``."""
    if reason := _skipped(source.server):
        return ("skipped", reason)
    registry, reason = await _connect(source, deadline)
    if registry is None:
        if reason[0] not in ("not_enabled", "not_configured", "no_client"):
            _fail(source.server, reason[1], limits)
        return reason
    offered = set(registry.get_tool_names())
    tools = [
        next(
            (
                f"{source.server}_{t}"
                for t in c.tools
                if f"{source.server}_{t}" in offered
            ),
            None,
        )
        for c in source.calls
    ]
    if any(t is None for t, c in zip(tools, source.calls) if c.required):
        names = ", ".join(t for c in source.calls if c.required for t in c.tools)
        return ("no_tool", f"offers none of {names}")

    async def one(key: str, value: str) -> Optional[Reason]:
        facts, why, failed = await _fetch_one(
            source, tools, registry, value, limits, deadline
        )
        if facts is not None:
            out[(key, source.server)] = facts
        elif failed:
            return why
        elif why:
            missed[(key, source.server)] = why
        return None

    failures = [r for r in await asyncio.gather(*(one(k, v) for k, v in items)) if r]
    if failures:
        _fail(source.server, failures[0][1], limits)
        return failures[0]
    return None


async def read(finding: Dict[str, Any]) -> FactRead:
    """The facts for a finding's users and hosts. Never raises and never waits past
    the budget: whatever a source has not answered by then is unavailable."""
    return await _guarded(
        lambda: candidates(finding),
        lambda: candidates(finding, None),
        {"finding_id": finding.get("finding_id")},
        None,
    )


def key_pairs(keys: List[str], kinds: Optional[set] = KINDS) -> List[Tuple[str, str]]:
    """(Entity Key, value) for the keys of ``kinds`` (None: every kind), in order.
    The value is the part after ``type:``, as the key spells it."""
    out, seen = [], set()
    for key in keys:
        kind, _, value = str(key).partition(":")
        if (kinds is None or kind in kinds) and value.strip() and key not in seen:
            seen.add(key)
            out.append((key, value.strip()))
    return out


class CaseUnreadable(Exception):
    """The caller may not read the Case the read names, or there is no such Case."""


def _case_refusal(case_id: str) -> Optional[str]:
    """Why the bound caller may not read ``case_id``'s rows, or None. A run's
    person reads as that person; a run with none as the case loaders read it."""
    from core.auth.tool_principal import current_claims
    from core.storage import case_read_hook
    from core.storage.models import Case
    from core.storage.unit_of_work import unit_of_work

    claims = current_claims()
    with unit_of_work() as session:
        case = session.get(Case, case_id)
        if case is None:
            return f"case {case_id} not found"
        allowed = (
            case_read_hook.may_read_as(case, claims.person_id)
            if claims is not None and claims.person_id
            else case_read_hook.may_read(case)
        )
    return None if allowed else f"this run may not read case {case_id}"


async def read_keys(
    keys: List[str], case_id: Optional[str] = None, declared_only: bool = False
) -> FactRead:
    """The facts for a run's trigger Entity Keys, on ``case_id``'s rows when it
    names one. Raises CaseUnreadable when the caller may not read that Case,
    and never falls back to triage's rows; otherwise as ``read``.
    ``declared_only`` (a hunt's read): the Declared rows alone, no source asked,
    nothing loaded, stored or put on the trail."""
    if declared_only:
        return await _guarded(lambda: [], lambda: key_pairs(keys, None), {}, None, True)
    if case_id and (why := await asyncio.to_thread(_case_refusal, case_id)):
        raise CaseUnreadable(why)
    return await _guarded(
        lambda: key_pairs(keys),
        lambda: key_pairs(keys, None),
        {"case_id": case_id},
        case_id,
    )


async def _guarded(
    asked: Callable[[], List[Tuple[str, str]]],
    every: Callable[[], List[Tuple[str, str]]],
    entry: Dict[str, Any],
    case_id: Optional[str],
    declared_only: bool = False,
) -> FactRead:
    result = FactRead()
    started = time.monotonic()
    # Beside the source read, not after it, so the two share one wait.
    declared = asyncio.ensure_future(asyncio.to_thread(lambda: declared_rows(every())))
    if not declared_only:
        try:
            await _read(asked(), result, entry, case_id)
        except Exception as exc:  # noqa: BLE001 - the reader answers with what was read
            logger.exception("entity facts not read")
            result.unavailable["entity facts"] = f"{type(exc).__name__}: {exc}"
    try:
        rows = await asyncio.wait_for(
            declared, _left(started + DEFAULTS.budget_seconds) or 0.1
        )
    except Exception as exc:  # noqa: BLE001 - the source rows stand without them
        logger.warning("declared site rows not read: %s", type(exc).__name__)
        return result
    result.facts.extend(rows)
    result.keys.extend(dict.fromkeys(f.key for f in rows if f.key not in result.keys))
    return result


# ---- Declared rows: the applied site revision (#5211) ----

DECLARED = "Declared"
_DECLARED_KIND = {
    "zones": "zone",
    "crown_jewels": "crown jewel",
    "owners": "owner",
    "blind_spots": "blind spot",
}


def _applied_at(row: Dict[str, Any]) -> datetime:
    """The revision's apply time, never the read's, so a re-read says the same."""
    try:
        return datetime.fromisoformat(str(row.get("applied_at"))).replace(tzinfo=None)
    except ValueError:
        return datetime(1970, 1, 1)


def declared_rows(pairs: List[Tuple[str, str]]) -> List[Fact]:
    """One Declared row per site item matching one of ``pairs`` (key, value):
    its kind, name, mission or what is unseen, zone, resolved owner, selector,
    file, line and revision. Blocking (the site row and teams): call off the loop."""
    from core import site
    from core.memory.entity_keys import normalise_key

    original: Dict[str, Tuple[str, str]] = {}
    for key, value in pairs:
        original.setdefault(normalise_key(key), (key, value))
    row = site.applied_site()
    matches = site.match(list(original), row)
    if not matches:
        return []
    teams = site._active_teams() if any(m["kind"] == "owners" for m in matches) else {}

    def owner(m: Dict[str, Any]) -> str:
        o = site.resolve_owner(m, teams)
        return o.get("resolved") or f"unassigned: {o['unassigned']}"

    owners: Dict[str, List[str]] = {}
    zones: Dict[str, List[str]] = {}
    for m in matches:
        if m["kind"] == "owners":
            owners.setdefault(m["key"], []).append(owner(m))
        elif m["kind"] == "zones":
            zones.setdefault(m["key"], []).append(m["name"])
    at = _applied_at(row)
    out = []
    for m in matches:
        kind = m["kind"]
        key, value = original.get(m["key"], (m["key"], m["key"].partition(":")[2]))
        who = [owner(m)] if kind == "owners" else owners.get(m["key"], [])
        where = (
            [m["zone"]]
            if m.get("zone")
            else [] if kind == "zones" else zones.get(m["key"], [])
        )
        fields = _clean(
            {
                "declared": _DECLARED_KIND[kind],
                "name": m.get("name"),
                "mission": m.get("mission"),
                "what": m.get("what"),
                "zone": ", ".join(dict.fromkeys(where)),
                "owner": ", ".join(dict.fromkeys(who)),
                "selector": m["selector"],
                "file": m.get("file"),
                "line": m.get("line"),
                "revision": m.get("revision"),
            }
        )
        out.append(Fact(key, value, "site", DECLARED, fields, at))
    return out


def _load_for(
    pairs: List[Tuple[str, str]], now: datetime, case_id: Optional[str]
) -> Tuple[Dict[Tuple[str, str], Row], List[StoredRow]]:
    """The rows served, and the caseless rows copied onto ``case_id`` with their
    facts, fetched_at and expiry, so a source is asked only for what neither has."""
    served = _load(pairs, now, case_id)
    if not case_id:
        return served, []
    copied = _load([p for p in pairs if p not in served], now)
    served.update(copied)
    return served, [(k, s, f, at, ex) for (k, s), (f, at, ex) in copied.items()]


async def _read(
    asked: List[Tuple[str, str]],
    result: FactRead,
    entry: Dict[str, Any],
    case_id: Optional[str],
) -> None:
    started = time.monotonic()
    if not asked:
        return
    result.keys = [k for k, _ in asked]  # until the limits say how many
    limits = DEFAULTS
    try:
        limits = await asyncio.wait_for(asyncio.to_thread(read_limits), 1.0)
    except asyncio.TimeoutError:
        logger.warning("%s not read in time; defaults apply", LIMITS_KEY)
    deadline = started + limits.budget_seconds
    result.dropped = max(0, len(asked) - limits.max_keys)
    asked = asked[: limits.max_keys]
    result.keys = [k for k, _ in asked]
    value_of = dict(asked)
    pairs = [
        (k, s.server)
        for k, _ in asked
        for s in SOURCES
        if k.split(":", 1)[0] in s.kinds
    ]
    served: Dict[Tuple[str, str], Row] = {}
    copied: List[StoredRow] = []
    try:
        served, copied = await asyncio.wait_for(
            asyncio.to_thread(_load_for, pairs, utcnow(), case_id), _left(deadline)
        )
    except Exception as exc:  # noqa: BLE001 - every pair is then a miss
        logger.warning("entity facts not loaded: %s", type(exc).__name__)

    misses: Dict[str, List[Tuple[str, str]]] = {}
    for key, server in pairs:
        if (key, server) not in served:
            misses.setdefault(server, []).append((key, value_of[key]))
    fetched: Dict[Tuple[str, str], Dict[str, Any]] = {}
    missed: Dict[Tuple[str, str], Reason] = {}
    reasons: Dict[str, Reason] = {}

    async def one(server: str) -> None:
        why = await _fetch(
            _BY_SERVER[server], misses[server], limits, deadline, fetched, missed
        )
        if why:
            reasons[server] = why

    tasks = {s: asyncio.create_task(one(s)) for s in misses}
    if tasks:
        _, pending = await asyncio.wait(tasks.values(), timeout=_left(deadline))
        for server, task in tasks.items():
            if task not in pending:
                continue
            task.cancel()
            reasons[server] = (
                "timeout",
                f"no answer within the {limits.budget_seconds}s budget",
            )
            # Slow is not failed: a source that answered some entities is asked again.
            if not any(s == server for _, s in fetched):
                _fail(server, reasons[server][1], limits)

    fetched_at = utcnow()
    for key, server in pairs:
        source = _BY_SERVER[server]
        counts = result.counts.setdefault(
            server, {"served": 0, "fetched": 0, "unavailable": 0}
        )
        if (key, server) in served:
            facts, at, _ = served[(key, server)]
            counts["served"] += 1
        elif (key, server) in fetched:
            facts, at = fetched[(key, server)], fetched_at
            counts["fetched"] += 1
        else:
            counts["unavailable"] += 1
            if (key, server) in missed:
                label = f"{source.label} for {value_of[key]}"
                result.unavailable[label] = missed[(key, server)][1]
            else:
                result.unavailable[source.label] = reasons.get(
                    server, ("", "not answered")
                )[1]
            continue
        if _not_found(facts):
            facts = {"record": "none found"}
        if facts:
            result.facts.append(
                Fact(key, value_of[key], server, source.label, facts, at)
            )

    # Stored and recorded after triage has its answer, so neither adds to the wait.
    rows: List[StoredRow] = copied + sorted(
        (k, s, f, fetched_at, None) for (k, s), f in fetched.items()
    )
    entry = {
        **entry,
        "rows_copied": len(copied),
        "keys_asked": len(result.keys),
        "keys_dropped": result.dropped,
        "sources": {
            server: {
                **counts,
                **({"reason": reasons[server][0]} if server in reasons else {}),
                **(
                    {"entities_refused": n}
                    if (n := sum(1 for (_, s) in missed if s == server))
                    else {}
                ),
            }
            for server, counts in result.counts.items()
        },
    }
    _spawn(asyncio.to_thread(_finish, rows, limits, entry, list(result.keys), case_id))


def _finish(
    rows: List[StoredRow],
    limits: FactLimits,
    entry: Dict[str, Any],
    keys: List[str],
    case_id: Optional[str] = None,
) -> None:
    """Store what was fetched or copied, evict past the cap, then the read's one
    trail entry."""
    try:
        entry["rows_evicted"] = _store(rows, limits, case_id) if rows else 0
    except Exception:  # noqa: BLE001 - the facts were shown; the next read refetches
        logger.exception("entity facts not stored")
        entry["store_failed"] = True
    _audit(ACTION_READ, entry, keys)


# ---- The sweep ----


def sweep() -> Dict[str, int]:
    """Delete expired rows and rows whose Case closed; one trail entry with the
    counts. Run from the daemon's cleanup sweep, off the event loop."""
    from core.storage.unit_of_work import unit_of_work

    now = utcnow()
    with unit_of_work() as session:
        expired = (
            session.execute(
                text("DELETE FROM public.entity_facts WHERE expires_at <= :now"),
                {"now": now},
            ).rowcount
            or 0
        )
        closed = session.execute(text("""
                DELETE FROM public.entity_facts f USING public.cases c
                WHERE f.case_id = c.case_id AND c.status = 'closed'
            """)).rowcount or 0
    counts = {"expired": expired, "case_closed": closed}
    _audit(ACTION_SWEEP, {"removed": counts})
    return counts


# ---- What is held: the operator's read (#4743) ----


def held(limit: int = 100) -> Dict[str, Any]:
    """Per source: rows held, oldest and newest fetch, and over the last TTL the hit
    rate and last failure kind from the ``entity_facts.read`` entries; the limits in
    force; and the newest ``limit`` rows. Rows on a Case the caller may not read are
    left out of the rows and of every count."""
    from sqlalchemy import func, or_, select

    from core.storage.case_read_hook import read_clause
    from core.storage.models import Case
    from core.storage.models.entity_fact import EntityFact as F
    from core.storage.unit_of_work import unit_of_work

    limits = read_limits()
    now = utcnow()
    since = now - timedelta(hours=limits.ttl_hours)

    def visible(q: Any) -> Any:
        return q.outerjoin(Case, Case.case_id == F.case_id).where(
            or_(F.case_id.is_(None), read_clause())
        )

    with unit_of_work() as session:
        counts = {
            source: (n, oldest, newest)
            for source, n, oldest, newest in session.execute(
                visible(
                    select(
                        F.source,
                        func.count(),
                        func.min(F.fetched_at),
                        func.max(F.fetched_at),
                    ).select_from(F)
                ).group_by(F.source)
            )
        }
        rows = (
            session.execute(
                visible(select(F).select_from(F))
                .order_by(F.fetched_at.desc(), F.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        rows = [
            {
                "entity_key": r.entity_key,
                "source": r.source,
                "case_id": r.case_id,
                "fields": r.facts,
                "fetched_at": r.fetched_at,
                "expires_at": r.expires_at,
                "hit_count": r.hit_count,
            }
            for r in rows
        ]
        # The read's own trail entries: the daemon's skip state is not visible here.
        reads = session.execute(
            text("""
                SELECT occurred_at, payload->'sources' FROM public.audit_trail
                WHERE payload->>'action' = :action AND occurred_at >= :since
                ORDER BY occurred_at, seq
            """),
            {"action": ACTION_READ, "since": since.replace(tzinfo=timezone.utc)},
        ).all()

    trail: Dict[str, Dict[str, Any]] = {}
    for at, sources in reads:
        for server, entry in (sources if isinstance(sources, dict) else {}).items():
            if not isinstance(entry, dict):
                continue
            t = trail.setdefault(
                server, {"served": 0, "fetched": 0, "unavailable": 0, "failure": None}
            )
            for k in ("served", "fetched", "unavailable"):
                v = entry.get(k)
                t[k] += v if isinstance(v, int) and not isinstance(v, bool) else 0
            if entry.get("reason"):
                # Naive UTC, as every other time here (core/time.py).
                t["failure"] = {
                    "at": at.astimezone(timezone.utc).replace(tzinfo=None),
                    "reason": str(entry["reason"]),
                }

    sources = []
    for s in SOURCES:
        n, oldest, newest = counts.get(s.server, (0, None, None))
        t = trail.get(
            s.server, {"served": 0, "fetched": 0, "unavailable": 0, "failure": None}
        )
        asked = t["served"] + t["fetched"] + t["unavailable"]
        sources.append(
            {
                "source": s.server,
                "label": s.label,
                "rows": n,
                "oldest_fetched_at": oldest,
                "newest_fetched_at": newest,
                "served": t["served"],
                "fetched": t["fetched"],
                "unavailable": t["unavailable"],
                "hit_rate": t["served"] / asked if asked else None,
                "last_failure": t["failure"],
            }
        )
    return {
        "now": now,
        "window_hours": limits.ttl_hours,
        "limits": {name: getattr(limits, name) for name in BOUNDS},
        "defaults": {name: getattr(DEFAULTS, name) for name in BOUNDS},
        "sources": sources,
        "rows_total": sum(n for n, _, _ in counts.values()),
        "rows": rows,
    }
