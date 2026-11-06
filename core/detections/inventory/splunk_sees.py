"""Sees for Splunk saved searches: whether the data each active search reads is arriving.

A search's data source is read from its first clause, after its macros are
expanded (definitions read once a sweep, through REST):

- ``index=`` / ``sourcetype=`` terms (``IN (…)`` and ORs too) -> ``index=X
  sourcetype=Y``. A search naming no index is judged against its owner's default
  search indexes, or against any index when those can't be read;
- ``| tstats … from datamodel=DM[.Dataset]``, ``| datamodel DM Dataset …`` or
  ``| from datamodel:DM.Dataset`` -> ``datamodel DM.Dataset``.

Anything else (a lookup, ``| rest``, a subsearch, an eventtype, a real-time
search, an undefined macro) reads ``unknown`` with the reason, never ``pass``.

Searches, each spent against the splunk integration's ``max_searches_per_hour``
with its own key, over the last 7 days: one ``tstats … by index, sourcetype``
answering every index/sourcetype source, and one ``tstats summariesonly=true …
by nodename`` per accelerated data model read (a model not accelerated is not
searched). Each key is searched at most once per 24 h by any Vigil process,
least recently searched first; this process holds its answers for that long, and
rules whose answer another process holds are left as they are, as are rules
whose search waits on the budget. A failed search is tried again on this
process's next sweep. Vigil's Splunk overlay copies in shadow or live (not
handed off to a Splunk saved search) are checked alongside, on the same keyed
searches and budget. Nothing is written to Splunk.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from sqlalchemy import and_, select

from core.detections.inventory import readers, sees, splunk
from core.integrations._base import search_budget
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

DAYS = sees.DAYS
HOLD = timedelta(hours=24)
RAW = "splunk:raw"
MAX_DEPTH = 20

RuleKey = sees.RuleKey
# ("raw", indexes, sourcetypes, note) or ("dm", model, dataset or None, None).
Need = Tuple[str, Any, Any, Optional[str]]

# (server url, budget key) -> (monotonic time searched, answer). A restart reads last_spent.
held: Dict[Tuple[str, str], Tuple[float, Any]] = {}
# Keys whose last search in this process failed: searched again on the next sweep.
failed: Set[Tuple[str, str]] = set()


@dataclass
class SplunkSeesReport:
    source: str = "splunk:sees"
    rules: int = 0
    searched: int = 0
    deferred: int = 0
    left: int = 0
    written: int = 0
    closed: int = 0
    error: Optional[str] = None


class _Unread(Exception):
    """The rule's data source can't be read; the message is why."""


def _unknown(reason: str, data_source: Optional[str] = None) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason, "data_source": data_source}


def _split(text: str, sep: str) -> List[str]:
    """``text`` split on ``sep`` outside quotes, brackets and parentheses."""
    parts, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and quote:
            i += 2
            continue
        if c == '"':
            quote = not quote
        elif not quote and c in "[(":
            depth += 1
        elif not quote and c in "])":
            depth = max(depth - 1, 0)
        elif not quote and depth == 0 and c == sep:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _macro_table(entries: List[Mapping[str, Any]]) -> Dict[str, List[Mapping]]:
    table: Dict[str, List[Mapping]] = {}
    for e in entries:
        table.setdefault(str(e.get("name")), []).append(e)
    return table


def _lookup(table: Dict[str, List[Mapping]], key: str, app: Optional[str]):
    """The definition the rule's app sees: its own app's, else a global one, else any."""
    found = table.get(key) or []
    for want in (
        lambda e: (e.get("acl") or {}).get("app") == app,
        lambda e: (e.get("acl") or {}).get("sharing") == "global",
        lambda e: True,
    ):
        for e in found:
            if want(e):
                return e.get("content") or {}
    return None


MACRO = re.compile(r"`([^`]*)`")
COMMENT = re.compile(r"```.*?```", re.S)


def expand(search: str, table: Dict[str, List[Mapping]], app: Optional[str]) -> str:
    """``search`` with every macro expanded, argument macros too; raises _Unread."""

    def one(match: re.Match) -> str:
        call = match.group(1).strip()
        name, args = call, []
        with_args = re.fullmatch(r"([^()\s]+)\s*\((.*)\)", call, re.S)
        if with_args:
            name = with_args.group(1)
            args = [a.strip() for a in _split(with_args.group(2), ",")]
        key = f"{name}({len(args)})" if args else name
        content = _lookup(table, key, app)
        if content is None:
            raise _Unread(f"macro `{key}` is not defined")
        if splunk._truthy(content.get("iseval")):
            raise _Unread(f"macro `{key}` is an eval macro: its expansion is not read")
        body = str(content.get("definition") or "")
        names = [a.strip() for a in str(content.get("args") or "").split(",")]
        for arg, value in zip(names, args):
            body = body.replace(f"${arg}$", value)
        return body

    search = COMMENT.sub(" ", search)
    for _ in range(MAX_DEPTH):
        if "`" not in search:
            return search
        search = MACRO.sub(one, search)
    raise _Unread(f"macros nest deeper than {MAX_DEPTH}")


TERM = re.compile(
    r'(?<![\w.])(NOT\s+)?(index|sourcetype)\s*(!=|=)\s*("(?:[^"\\]|\\.)*"|[^\s()\[\]]+)',
    re.I,
)
TERM_IN = re.compile(r"(?<![\w.])(NOT\s+)?(index|sourcetype)\s+IN\s*\(([^)]*)\)", re.I)
NOT_GROUP = re.compile(r"\bNOT\s*\([^()]*\)", re.I)


def _terms(clause: str) -> Dict[str, List[str]]:
    """The index and sourcetype values a clause includes (exclusions dropped)."""
    clause = NOT_GROUP.sub(" ", clause)
    out: Dict[str, List[str]] = {"index": [], "sourcetype": []}
    for neg, field, op, value in TERM.findall(clause):
        if not neg and op == "=":
            out[field.lower()].append(value.strip('"'))
    for neg, field, values in TERM_IN.findall(clause):
        if not neg:
            out[field.lower()].extend(
                v.strip().strip('"') for v in values.split(",") if v.strip()
            )
    return {k: sorted(set(v)) for k, v in out.items()}


def _dataset(value: str) -> Tuple[str, Optional[str]]:
    model, _, dataset = value.strip('"').partition(".")
    return model, dataset or None


def need_of(search: str) -> Need:
    """The search's data source from its first clause; raises _Unread."""
    parts = _split(search.strip(), "|")
    if parts[0].strip():
        clause = re.sub(r"^\s*search\b", "", parts[0], flags=re.I)
        if "[" in clause:
            raise _Unread(
                "a subsearch in its first clause: its data source is not read"
            )
        if re.search(r"(?<![\w.])eventtype\s*=", clause, re.I):
            raise _Unread("eventtype= in its first clause is not resolved")
        terms = _terms(clause)
        if not terms["index"] and not terms["sourcetype"]:
            raise _Unread("its first clause names no index or sourcetype")
        return "raw", terms["index"], terms["sourcetype"], None
    command = parts[1].strip() if len(parts) > 1 else ""
    word = (command.split() or [""])[0].lower()
    if word == "tstats":
        m = re.search(r'\bfrom\s+datamodel\s*[=:]\s*("[^"]+"|[\w.:-]+)', command, re.I)
        if m:
            return ("dm", *_dataset(m.group(1)), None)
        if re.search(r"\bfrom\b", command, re.I):
            raise _Unread(
                "| tstats from a namespace or sid: its data source is not read"
            )
        terms = _terms(command)
        if not terms["index"]:
            raise _Unread("| tstats names no index or data model")
        return "raw", terms["index"], terms["sourcetype"], None
    if word == "datamodel":
        tokens = command.split()
        if len(tokens) < 2:
            raise _Unread("| datamodel names no data model")
        model = tokens[1]
        dataset = (
            tokens[2]
            if len(tokens) > 2 and tokens[2] not in ("search", "flat")
            else None
        )
        return "dm", model, dataset, None
    if word == "from":
        m = re.search(r'\bdatamodel:("[^"]+"|[\w.-]+)', command, re.I)
        if m:
            return ("dm", *_dataset(m.group(1)), None)
        raise _Unread("| from reads no data model: its data source is not read")
    if word == "inputlookup":
        raise _Unread("| inputlookup reads a lookup, not events")
    if not word:
        raise _Unread("the search has no generating clause")
    raise _Unread(f"| {word} reads no index: its data source is not read")


def _match(value: str, pattern: str) -> bool:
    """Splunk's matching: case-insensitive, and ``*`` covers no internal index."""
    value, pattern = value.lower(), pattern.lower()
    return fnmatch.fnmatchcase(value, pattern) and not (
        value.startswith("_") and not pattern.startswith("_")
    )


def _fmt(field: str, values: List[str]) -> str:
    return (
        f"{field}={values[0]}"
        if len(values) == 1
        else f"{field} IN ({', '.join(values)})"
    )


def _source(need: Need) -> str:
    kind, a, b, _ = need
    if kind == "dm":
        return f"datamodel {a}.{b}" if b else f"datamodel {a}"
    return " ".join([_fmt("index", a)] + ([_fmt("sourcetype", b)] if b else []))


def _key(need: Need) -> str:
    return RAW if need[0] == "raw" else f"splunk:dm:{need[1]}"


def _rules() -> List[Tuple[RuleKey, Mapping[str, Any]]]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRuleVersion.content,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(
                DetectionRule.source == splunk.SOURCE, DetectionRule.status == "active"
            )
            .order_by(DetectionRule.id)
        ).all()
    return [((rid, ver), content or {}) for rid, ver, content in rows]


def _needs(
    service: Any, rules: List[Tuple[RuleKey, Mapping[str, Any]]]
) -> Tuple[Dict[RuleKey, Need], Dict[RuleKey, Dict[str, Any]]]:
    """Each rule's data source, or its unknown result. REST reads only."""
    needs: Dict[RuleKey, Need] = {}
    results: Dict[RuleKey, Dict[str, Any]] = {}
    table: Optional[Dict[str, List[Mapping]]] = None
    macro_error: Optional[str] = None
    owners: Dict[str, Optional[List[str]]] = {}
    for key, content in rules:
        search = str(content.get("search") or "")
        if str(content.get("dispatch.earliest_time") or "").startswith("rt"):
            results[key] = _unknown("real-time search: its data source is not checked")
            continue
        try:
            if "`" in search:
                if table is None and macro_error is None:
                    try:
                        table = _macro_table(service.macros())
                    except Exception as exc:  # noqa: BLE001 -- macro rules read unknown
                        macro_error = f"macros could not be read: {exc}"
                if macro_error:
                    raise _Unread(macro_error)
                search = expand(search, table or {}, content.get("app"))
            need = need_of(search)
        except _Unread as exc:
            results[key] = _unknown(str(exc))
            continue
        if need[0] == "raw" and not need[1]:
            owner = str(content.get("owner") or "")
            if owner not in owners:
                owners[owner] = service.default_indexes(owner) if owner else None
            defaults = owners[owner]
            if defaults == []:
                results[key] = _unknown(
                    f"no index named, and {owner}'s roles set no default search index"
                )
                continue
            note = (
                f"no index named: judged against {owner}'s default search indexes"
                if defaults
                else "no index named and its owner's default search indexes could "
                "not be read: judged against any index"
            )
            need = ("raw", defaults or ["*"], need[2], note)
        needs[key] = need
    return needs, results


def _internal(needs: List[Need]) -> bool:
    return any(i.startswith("_") for n in needs for i in n[1])


def _search(service: Any, key: str, needs: List[Need]) -> Any:
    """One keyed search's answer: (index, sourcetype) pairs, or a model's nodenames."""
    earliest = f"-{DAYS}d"
    if key == RAW:
        internal = _internal(needs)
        where = "(index=* OR index=_*)" if internal else "index=*"
        rows = service.search(
            f"| tstats count where {where} by index, sourcetype",
            earliest_time=earliest,
            max_count=0,
        ).results
        pairs = {
            (str(r.get("index")), str(r.get("sourcetype")))
            for r in rows
            if int(r.get("count") or 0)
        }
        return pairs, internal
    model = key.split(":", 2)[2]
    rows = service.search(
        f"| tstats summariesonly=true count from datamodel={model} by nodename",
        earliest_time=earliest,
        max_count=0,
    ).results
    return {str(r.get("nodename")) for r in rows if int(r.get("count") or 0)}


def _verdict(need: Need, answer: Any, allowed: Any) -> Dict[str, Any]:
    source = _source(need)
    if isinstance(answer, str):
        return _unknown(answer, source)
    kind, a, b, note = need
    if kind == "dm":
        seen = any(
            b is None or f".{n}.".find(f".{b}.") >= 0 for n in answer
        )  # the dataset, or a child of it
    else:
        seen = any(
            any(_match(i, p) for p in a) and (not b or any(_match(s, p) for p in b))
            for i, s in answer[0]
        )
    if seen:
        return {"result": "pass", "reason": note, "data_source": source}
    if kind == "raw":
        concrete = [i for i in a if "*" not in i]
        if concrete and len(concrete) == len(a) and all(allowed(i) is False for i in a):
            return {
                **_unknown(
                    f"Vigil's Splunk user cannot search {_fmt('index', a)}", source
                ),
                "unreadable": True,  # rollout holds an overlay copy's runs
            }
    reason = f"fail: no events in {source} in {DAYS}d"
    return {
        "result": "fail",
        "reason": f"{reason} ({note})" if note else reason,
        "data_source": source,
    }


def _check(
    service: Any,
    rules: List[Tuple[RuleKey, Mapping[str, Any]]],
    url: str,
    per_hour: int,
    report: SplunkSeesReport,
) -> Dict[RuleKey, Dict[str, Any]]:
    needs, results = _needs(service, rules)
    answers: Dict[str, Any] = {}
    by_key: Dict[str, List[Need]] = {}
    for need in needs.values():
        by_key.setdefault(_key(need), []).append(need)

    models: Optional[Dict[str, bool]] = None
    model_error: Optional[str] = None
    now = time.monotonic()
    due = []
    for key in by_key:
        if key != RAW:
            model = key.split(":", 2)[2]
            if models is None and model_error is None:
                try:
                    models = service.datamodels()
                except Exception as exc:  # noqa: BLE001 -- its rules read unknown
                    model_error = f"data models could not be read: {exc}"
            if model_error:
                answers[key] = model_error
                continue
            if model not in (models or {}):
                answers[key] = f"data model {model} is not defined"
                continue
            if not models[model]:
                answers[key] = (
                    f"data model {model} is not accelerated; "
                    "not searched to spare the SIEM"
                )
                continue
        hold = held.get((url, key))
        # A raw answer without the internal indexes doesn't answer a rule reading one.
        stale = key == RAW and hold and _internal(by_key[key]) and not hold[1][1]
        if hold and not stale and now - hold[0] < HOLD.total_seconds():
            answers[key] = hold[1]
        else:
            due.append(key)
            if stale:
                failed.add((url, key))  # this process's own answer: search again

    last = search_budget.last_spent(splunk.SOURCE, due)
    never = datetime.min.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - HOLD
    # Searched within 24 h by a process whose answer this one doesn't hold: left as is.
    due = [k for k in due if last.get(k, never) <= cutoff or (url, k) in failed]
    for n, key in enumerate(sorted(due, key=lambda k: last.get(k, never))):
        if not search_budget.spend(splunk.SOURCE, per_hour, key):
            report.deferred = len(due) - n
            break
        report.searched += 1
        try:
            answers[key] = _search(service, key, by_key[key])
            held[(url, key)] = (time.monotonic(), answers[key])
            failed.discard((url, key))
        except Exception as exc:  # noqa: BLE001 -- its rules read unknown
            answers[key] = f"search failed: {type(exc).__name__}: {exc}"
            failed.add((url, key))

    allowed_cache: Dict[str, Optional[bool]] = {}

    def allowed(index: str) -> Optional[bool]:
        if index not in allowed_cache:
            allowed_cache[index] = service.can_search_index(index)
        return allowed_cache[index]

    for rule, need in needs.items():
        key = _key(need)
        if key not in answers:
            report.left += 1  # waits for the budget, or another process holds it
            continue
        results[rule] = _verdict(need, answers[key], allowed)
    return results


def sweep(config: Mapping[str, Any]) -> SplunkSeesReport:
    """Check Sees for the active Splunk saved searches and the stageful Splunk
    overlay copies, as far as the hour's budget goes."""
    report = SplunkSeesReport()
    try:
        rules = _rules()
        try:
            copies = sees.overlay_copies("splunk", splunk.SOURCE)
        except Exception as exc:  # noqa: BLE001 -- the saved searches still get Sees
            logger.warning("detection rules: overlay copies unread for Sees: %s", exc)
            report.error = f"overlay: {type(exc).__name__}: {exc}"
            copies = []
        else:  # left-out copies close only when the selection was read
            report.closed = sees.close_unchecked("splunk", {k[0] for k, _ in copies})
        report.rules = len(rules) + len(copies)
        service = splunk.service_of(config)
        results = _check(
            service,
            rules + copies,
            service.server_url,
            splunk.per_hour_of(config),
            report,
        )
        report.written = sees.write_check(
            "sees",
            {k: results[k] for k, _ in rules if k in results},
            source=splunk.SOURCE,
        ) + sees.write_check(
            "sees",
            {k: results[k] for k, _ in copies if k in results},
            source=readers.OVERLAY_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Splunk Sees sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Splunk Sees %s", report)
    return report
