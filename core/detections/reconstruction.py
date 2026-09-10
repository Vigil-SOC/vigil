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
