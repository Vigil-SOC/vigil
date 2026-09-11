"""Correlate a red-run action trace to ingested Findings.

Each step is joined on host / entity / time fields that are actually present
on the Finding. Technique ids and other unknown keys on a step are ignored.
Verdicts: rule | loglm | both | missed. LogLM-origin is ``data_source ==
"loglm"``; any other correlated Finding is rule-origin.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

Verdict = Literal["rule", "loglm", "both", "missed"]

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


_STEP_KEYS = (
    "started_at",
    "start",
    "start_time",
    "timestamp",
    "hostname",
    "host",
    "computer_name",
    "src_ip",
    "src_ips",
    "user",
    "id",
    "step_id",
    "technique_id",
)
_EVIDENCE_KEYS = (
    "hostname",
    "host",
    "computer_name",
    "src_ip",
    "user",
    "command",
)


def steps_from_dispatch_results(results: Any) -> List[Dict[str, Any]]:
    """Pull action-trace steps out of journaled gated-execute ToolResults."""
    if isinstance(results, dict):
        held = results.get("results")
        if isinstance(held, list):
            results = held
        else:
            return _steps_from_result(results)
    if not isinstance(results, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in results:
        out.extend(_steps_from_result(item))
    return out


def coverage_report(trace: Any, reconstructed: Dict[str, Any]) -> Dict[str, Any]:
    """Group reconstructed verdicts by technique_id; join on step id / index."""
    records = reconstructed.get("steps") if isinstance(reconstructed, dict) else None
    if not isinstance(records, list) or not isinstance(trace, list):
        return {"techniques": []}

    by_id: Dict[Any, Dict[str, Any]] = {}
    by_index: Dict[Any, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        step_id = record.get("id")
        if step_id not in (None, ""):
            by_id[step_id] = record
        by_index[record.get("index")] = record

    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for index, raw in enumerate(trace):
        record = _joined_record(raw, index, by_id, by_index)
        if record is None:
            continue
        tid = raw.get("technique_id") if isinstance(raw, dict) else None
        if not isinstance(tid, str) or not tid.strip():
            continue
        tid = tid.strip().upper()
        if tid not in grouped:
            grouped[tid] = {"technique_id": tid, "steps": [], "missed": []}
            order.append(tid)
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
            }
        )
    return {"techniques": techniques}


def _joined_record(
    raw: Any,
    index: int,
    by_id: Dict[Any, Dict[str, Any]],
    by_index: Dict[Any, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    step_id = None
    if isinstance(raw, dict):
        step_id = raw.get("id") or raw.get("step_id")
    if step_id not in (None, "") and step_id in by_id:
        return by_id[step_id]
    return by_index.get(index)


def _layer_verdict(verdicts: List[Any]) -> Verdict:
    has_rule = False
    has_loglm = False
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


def _steps_from_result(item: Any) -> List[Dict[str, Any]]:
    if isinstance(item, list):
        return _steps_from_rows(item)
    if not isinstance(item, dict):
        return []
    rows = item.get("rows")
    if isinstance(rows, list) and ("ok" in item or "rowCount" in item):
        return _steps_from_rows(rows)
    if isinstance(item.get("steps"), list):
        return [row for row in item["steps"] if isinstance(row, dict)]
    if _looks_like_step(item):
        return [item]
    return []


def _steps_from_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("steps")
        if isinstance(nested, list):
            steps.extend(step for step in nested if isinstance(step, dict))
        elif _looks_like_step(row):
            steps.append(row)
    return steps


def _looks_like_step(row: Dict[str, Any]) -> bool:
    return any(key in row for key in _STEP_KEYS)
