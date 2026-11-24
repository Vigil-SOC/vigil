"""Correlate a red-run action trace to ingested Findings.

Each step is joined on host / entity / time fields that are actually present
on the Finding. Technique ids and other unknown keys on a step are ignored.
Verdicts: rule | loglm | both | missed. LogLM-origin is ``data_source ==
"loglm"``; any other correlated Finding is rule-origin.

``coverage_report`` then applies the one step rule every reader of an emulation
gets: a step a rule-origin Finding cites is caught whatever its exit; any other
step is ``unknown`` (with a ``reason``), never missed, when its runner reported an
error or a null or non-zero exit, or when it ended less than ``LAG`` ago.

A run's steps are read by ``steps_from_dispatch_results`` from emulator answers
only (docs/emulators.md), so they correlate on hostname and window alone.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Literal, Optional, Set, Tuple

Verdict = Literal["rule", "loglm", "both", "missed", "unknown"]

# How late an event may be indexed and still be seen (rollout.LAG reads this).
LAG = timedelta(minutes=30)

_LOG_LM = "loglm"

_START_KEYS = ("started_at", "start", "start_time", "timestamp")
_END_KEYS = ("ended_at", "end", "end_time")


def reconstruct(steps: Any, findings: Any) -> Dict[str, Any]:
    """Return one reconstruction record: a verdict per step, with citations."""
    if not isinstance(steps, list):
        return {"error": "steps must be a list of action-trace objects"}

    rows = findings if isinstance(findings, list) else []
    parsed: List[Dict[str, Any]] = []
    for finding in rows:
        if isinstance(finding, dict):
            parsed.append(finding)

    results: List[Dict[str, Any]] = []
    for index, raw in enumerate(steps):
        results.append(_reconstruct_step(index, raw, parsed))
    return {"steps": results}


def span_for_steps(steps: Any) -> Optional[Tuple[datetime, datetime]]:
    """Smallest window covering every step that has a parseable time range."""
    if not isinstance(steps, list):
        return None
    starts: List[datetime] = []
    ends: List[datetime] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        window = _window(raw)
        if window is None:
            continue
        starts.append(window[0])
        ends.append(window[1])
    if not starts:
        return None
    return min(starts), max(ends)


def _reconstruct_step(
    index: int, raw: Any, findings: List[Dict[str, Any]]
) -> Dict[str, Any]:
    record: Dict[str, Any] = {"index": index, "verdict": "missed", "citations": []}
    if not isinstance(raw, dict):
        return record

    step_id = raw.get("id") or raw.get("step_id")
    if step_id is not None and step_id != "":
        record["id"] = step_id

    window = _window(raw)
    step_entities = _entities_from_step(raw)
    if window is None or not _has_entity(step_entities):
        return record

    start, end = window
    hits: List[Dict[str, Any]] = []
    for finding in findings:
        if _correlates(finding, start, end, step_entities):
            hits.append(finding)

    hits.sort(key=lambda item: str(item.get("finding_id") or ""))
    record["verdict"] = _verdict(hits)
    record["citations"] = [_cite(finding) for finding in hits]
    return record


def _correlates(
    finding: Dict[str, Any],
    start: datetime,
    end: datetime,
    step_entities: Dict[str, List[str]],
) -> bool:
    finding_ts = _parse_time(finding.get("timestamp"))
    if finding_ts is None or finding_ts < start or finding_ts > end:
        return False
    context = finding.get("entity_context")
    finding_entities = _entities_from_context(
        context if isinstance(context, dict) else None
    )
    return _overlaps(step_entities, finding_entities)


def _verdict(hits: List[Dict[str, Any]]) -> Verdict:
    has_loglm = False
    has_rule = False
    for finding in hits:
        if finding.get("data_source") == _LOG_LM:
            has_loglm = True
        else:
            has_rule = True
    if has_loglm and has_rule:
        return "both"
    if has_loglm:
        return "loglm"
    if has_rule:
        return "rule"
    return "missed"


def _cite(finding: Dict[str, Any]) -> Dict[str, Any]:
    citation: Dict[str, Any] = {"finding_id": finding.get("finding_id") or ""}
    data_source = finding.get("data_source")
    if isinstance(data_source, str) and data_source:
        citation["data_source"] = data_source
    description = finding.get("description")
    if isinstance(description, str) and description:
        citation["description"] = description
    context = finding.get("entity_context")
    if isinstance(context, dict):
        rule_name = context.get("rule_name")
        if isinstance(rule_name, str) and rule_name.strip():
            citation["rule_name"] = rule_name
    return citation


def _window(step: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    start = _first_time(step, _START_KEYS)
    end = _first_time(step, _END_KEYS)
    if start is None and end is None:
        return None
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return start, end


def _first_time(step: Dict[str, Any], keys: Iterable[str]) -> Optional[datetime]:
    for key in keys:
        parsed = _parse_time(step.get(key))
        if parsed is not None:
            return parsed
    return None


def _entities_from_context(
    entity_context: Optional[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Host / IP / user values, using ``build_entity_string`` spellings."""
    if not entity_context:
        return {"hostnames": [], "src_ips": [], "users": []}

    src_ips = entity_context.get("src_ips") or []
    if not src_ips and entity_context.get("src_ip"):
        src_ips = [entity_context["src_ip"]]
    hostnames = entity_context.get("hostnames") or []
    if not hostnames and entity_context.get("hostname"):
        hostnames = [entity_context["hostname"]]
    users = entity_context.get("users") or entity_context.get("usernames") or []
    if not users and entity_context.get("user"):
        users = [entity_context["user"]]

    return {
        "hostnames": _as_values(hostnames),
        "src_ips": _as_values(src_ips),
        "users": _as_values(users),
    }


def _entities_from_step(step: Dict[str, Any]) -> Dict[str, List[str]]:
    hostnames = (
        step.get("hostnames")
        or step.get("hostname")
        or step.get("host")
        or step.get("computer_name")
        or []
    )
    src_ips = step.get("src_ips") or []
    if not src_ips and step.get("src_ip"):
        src_ips = [step["src_ip"]]
    users = step.get("users") or step.get("usernames") or []
    if not users:
        user = step.get("user") or step.get("username")
        users = [user] if user else []
    return {
        "hostnames": _as_values(hostnames),
        "src_ips": _as_values(src_ips),
        "users": _as_values(users),
    }


def _has_entity(entities: Dict[str, List[str]]) -> bool:
    return any(entities[key] for key in ("hostnames", "src_ips", "users"))


def _overlaps(
    step_entities: Dict[str, List[str]], finding_entities: Dict[str, List[str]]
) -> bool:
    for key in ("hostnames", "src_ips", "users"):
        if _folded(step_entities[key]) & _folded(finding_entities[key]):
            return True
    return False


def _folded(values: List[str]) -> set[str]:
    return {value.casefold() for value in values if value}


def _as_values(value: Any) -> List[str]:
    if value is None or isinstance(value, (dict, bool)):
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            if item is None or isinstance(item, (dict, bool)) or item == "":
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return None
        number = float(value)
        if abs(number) > 1e12:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _naive_utc(parsed)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


_EVIDENCE_KEYS = (
    "hostname",
    "host",
    "computer_name",
    "src_ip",
    "user",
    "command",
)


# What an emulator's step is read for (docs/emulators.md); the rest is evidence only.
STEP_FIELDS = ("technique_id", "hostname", "started_at", "ended_at", "exit", "id")


def answer_steps(answer: Any) -> List[Dict[str, Any]]:
    """The steps an emulator's answer holds: each object's ``steps``, or a bare
    step. The one reader of the shape: the host check and the report both use it."""
    found: List[Dict[str, Any]] = []
    for item in answer if isinstance(answer, list) else [answer]:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("steps"), list):
            found += [s for s in item["steps"] if isinstance(s, dict)]
        elif "technique_id" in item:
            found.append(item)
    return found


@dataclass
class EmulatedSteps:
    """A run's emulated steps, each once, and what was read but never scored."""

    steps: List[Dict[str, Any]] = field(default_factory=list)
    set_aside: List[Dict[str, Any]] = field(default_factory=list)
    outside_hosts: List[str] = field(default_factory=list)
    # An answer the row limit cut; a handle whose latest answer said done: false.
    capped: bool = False
    unfinished: List[str] = field(default_factory=list)

    def notes(self) -> Dict[str, Any]:
        return {
            "set_aside": self.set_aside,
            "outside_hosts": self.outside_hosts,
            "capped": self.capped,
            "unfinished": self.unfinished,
        }


def _set_aside_reason(step: Dict[str, Any], listed: Any) -> Optional[str]:
    from core.detections.gaps import TECHNIQUE_ID

    tid = step.get("technique_id")
    if not isinstance(tid, str) or not TECHNIQUE_ID.match(tid.strip()):
        return f"technique {tid!r} is not an ATT&CK technique id"
    host = step.get("hostname")
    if not isinstance(host, str) or not host.strip():
        return "it names no hostname"
    if listed(host.strip()) is None:
        return f"hostname {host.strip()!r} is not listed for emulation"
    if _window(step) is None:
        return "it names no time"
    return None


def steps_from_dispatch_results(projection: Any) -> EmulatedSteps:
    """The steps of a run's emulator answers: only results the agent layer stamped
    as an emulator's, only ``STEP_FIELDS`` of each. A step with no hostname, no
    time, a technique that is not an ATT&CK id or a host not listed now is set
    aside with its reason, never scored; a step reported twice counts once (by
    ``id``, else technique, host and start), its latest report kept."""
    from core.response.emulation import read_emulation

    out = EmulatedSteps()
    emulated = projection.get("emulated") if isinstance(projection, dict) else None
    if not isinstance(emulated, list) or not emulated:
        return out
    emulation = read_emulation()
    kept: Dict[Any, Dict[str, Any]] = {}
    aside: Dict[Tuple[str, str], Dict[str, Any]] = {}
    outside: Dict[str, str] = {}
    done: Dict[str, Any] = {}
    for entry in emulated:
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        out.capped = out.capped or result.get("capped") is True
        rows = result.get("rows")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("handle") not in (None, ""):
                done[str(row["handle"])] = row.get("done")
        for raw in answer_steps(rows):
            step = {k: raw[k] for k in STEP_FIELDS if k in raw}
            reason = _set_aside_reason(step, emulation.listed)
            if reason is not None:
                host = step.get("hostname")
                if reason.startswith("hostname"):
                    outside.setdefault(host.strip().lower(), host.strip())
                aside[(reason, repr(sorted(step.items(), key=str)))] = {
                    **step,
                    "tool": entry.get("tool"),
                    "reason": reason,
                }
                continue
            step["technique_id"] = step["technique_id"].strip().upper()
            step_id = step.get("id")
            key = (
                ("id", str(step_id))
                if step_id not in (None, "")
                else (
                    step["technique_id"],
                    step["hostname"].strip().lower(),
                    _window(step)[0],  # type: ignore[index]
                )
            )
            kept[key] = step
    out.steps = list(kept.values())
    out.set_aside = list(aside.values())
    out.outside_hosts = list(outside.values())
    out.unfinished = [h for h, d in done.items() if d is False]
    return out


def coverage_report(
    trace: Any,
    reconstructed: Dict[str, Any],
    now: Optional[datetime] = None,
    lag: timedelta = LAG,
) -> Dict[str, Any]:
    """Group reconstructed verdicts by technique_id; join on step id / index.

    Each step is judged by ``judge_step`` against ``now`` (naive or aware UTC).
    """
    records = reconstructed.get("steps") if isinstance(reconstructed, dict) else None
    if not isinstance(records, list) or not isinstance(trace, list):
        return {"techniques": []}
    now = _naive_utc(now) if now else datetime.now(timezone.utc).replace(tzinfo=None)

    by_id: Dict[Any, Deque[Dict[str, Any]]] = defaultdict(deque)
    by_index: Dict[Any, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        step_id = record.get("id")
        if step_id not in (None, ""):
            by_id[step_id].append(record)
        by_index[record.get("index")] = record

    used: Set[int] = set()
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for index, raw in enumerate(trace):
        record = _joined_record(raw, index, by_id, by_index, used)
        if record is None:
            continue
        tid = raw.get("technique_id") if isinstance(raw, dict) else None
        if not isinstance(tid, str) or not tid.strip():
            continue
        tid = tid.strip().upper()
        if tid not in grouped:
            grouped[tid] = {"technique_id": tid, "steps": [], "missed": []}
            order.append(tid)
        record = judge_step(raw, record, now, lag, reconstructed.get("cut_off"))
        grouped[tid]["steps"].append(record)
        if record.get("verdict") == "missed":
            grouped[tid]["missed"].append(_missed_step(raw, record))

    techniques = []
    for tid in order:
        group = grouped[tid]
        techniques.append(
            {
                "technique_id": tid,
                "verdict": _layer_verdict(
                    [step.get("verdict") for step in group["steps"]]
                ),
                "steps": group["steps"],
                "missed": group["missed"],
                "caught_in": _caught_in(group["steps"]),
            }
        )
    return {"techniques": techniques}


def judge_step(
    raw: Any,
    record: Dict[str, Any],
    now: datetime,
    lag: timedelta = LAG,
    cut_off: Optional[str] = None,
) -> Dict[str, Any]:
    """The step rule: the record as scored, with the step's hostname and times.

    Rule-origin citations make it caught whatever its exit. Otherwise a runner
    error, a null or non-zero ``exit`` (only when the step carries ``exit``: a
    hand-written trace has none), an end within ``lag`` of ``now``, a step with no
    time or no host/IP/user to join on, or a cut-off Findings read is unknown.
    """
    out = dict(record)
    if not isinstance(raw, dict):
        return out
    hosts = _entities_from_step(raw)["hostnames"]
    if hosts:
        out["hostname"] = hosts[0]
    window = _window(raw)
    if window is not None:
        out["started_at"], out["ended_at"] = (t.isoformat() for t in window)
    if out.get("verdict") in ("rule", "both"):
        return out
    reason = None
    error = raw.get("error")
    if error:
        reason = f"the runner reported an error ({str(error)[:200]})"
    elif "exit" in raw and raw["exit"] is None:
        reason = "the runner reported no exit code"
    elif "exit" in raw and raw["exit"] != 0:
        reason = f"the runner exited {raw['exit']}, so the step may not have run"
    elif window is None or not _has_entity(_entities_from_step(raw)):
        reason = "it names no time or no host, IP or user, so no Finding can join it"
    elif window[1] + lag > now:
        reason = (
            f"it ended under {int(lag.total_seconds() // 60)} minutes ago; "
            "alerts may still be arriving"
        )
    elif cut_off:
        reason = cut_off
    if reason:
        out["verdict"] = "unknown"
        out["reason"] = reason
    return out


def _caught_in(steps: List[Dict[str, Any]]) -> List[str]:
    """Distinct non-LogLM data_sources cited across a technique's steps."""
    sources = {
        cite.get("data_source")
        for step in steps
        for cite in step.get("citations") or []
        if isinstance(cite, dict)
    }
    return sorted(s for s in sources if isinstance(s, str) and s and s != _LOG_LM)


def _joined_record(
    raw: Any,
    index: int,
    by_id: Dict[Any, Deque[Dict[str, Any]]],
    by_index: Dict[Any, Dict[str, Any]],
    used: Set[int],
) -> Optional[Dict[str, Any]]:
    step_id = None
    if isinstance(raw, dict):
        step_id = raw.get("id") or raw.get("step_id")
    if step_id not in (None, ""):
        queue = by_id.get(step_id)
        while queue:
            candidate = queue.popleft()
            marker = id(candidate)
            if marker in used:
                continue
            used.add(marker)
            return candidate
    candidate = by_index.get(index)
    if candidate is not None and id(candidate) not in used:
        used.add(id(candidate))
        return candidate
    return None


def _layer_verdict(verdicts: List[Any]) -> Verdict:
    has_rule = False
    has_loglm = False
    if verdicts and all(v == "unknown" for v in verdicts):
        return "unknown"
    for verdict in verdicts:
        rule, loglm = _step_layers(verdict)
        has_rule = has_rule or rule
        has_loglm = has_loglm or loglm
    if has_rule and has_loglm:
        return "both"
    if has_loglm:
        return "loglm"
    if has_rule:
        return "rule"
    return "missed"


def _step_layers(verdict: Any) -> Tuple[bool, bool]:
    if verdict == "rule":
        return True, False
    if verdict == "loglm":
        return False, True
    if verdict == "both":
        return True, True
    if verdict == "missed":
        return False, False
    return False, False


def _missed_step(raw: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    missed: Dict[str, Any] = {
        "index": record.get("index"),
        "citations": record.get("citations") or [],
    }
    step_id = record.get("id")
    if step_id not in (None, ""):
        missed["id"] = step_id
    if not isinstance(raw, dict):
        return missed
    for key in _EVIDENCE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            missed[key] = value
    return missed
