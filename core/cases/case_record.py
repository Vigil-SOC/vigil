"""One newest-first read of a case's record.

Each run's ledger comes from the agent. ``case_audit_logs`` is read here, by
``entity_type`` and ``entity_id``: the table has no ``case_id`` column. The
case's own history is ``cases.activities``, appended to by runs, analysts and
merges; it is read off the case, not written here. This view does not query
``agent_events``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.storage.models import CaseAuditLog


def _at(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse(value: Any) -> Optional[datetime]:
    """A timestamp as an aware datetime; None when it is not one.

    Writers disagree: runs send ``...123Z``, activities naive UTC plus ``Z``,
    ``_at`` ``+00:00``. As strings they do not sort together.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _short(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= 280 else text[:277] + "..."


_MARKUP = re.compile(r"(^|\s)(#{1,6}|[-*+]|\d+[.)])\s+|\*\*|__|`")


def _plain(value: Any) -> str:
    """A description written as markdown (a run's report) as one plain line."""
    return _short(_MARKUP.sub(lambda m: m.group(1) or "", str(value or "")))


def _join(*parts: Any) -> str:
    kept = [
        str(part).strip() for part in parts if part is not None and str(part).strip()
    ]
    return " — ".join(kept)


# Kernel kinds (#2266) a person need not read: a model call's input and its bytes,
# its price, and the counters a turn moves. The ledger keeps them; the record does not
# print them.
_QUIET_KINDS = frozenset({"frame", "content", "spend", "finalize"})

# The hunt's own calls whose effect is a row of its own (the evidence, the
# explanation, the link, the lead, the question put to you), so the call itself says
# nothing new. Shown anyway when it was refused.
_SHOWN_ELSEWHERE = frozenset(
    {
        "record_evidence",
        "propose_hypothesis",
        "link_evidence",
        "open_question",
        "close_question",
        "ask_operator",
        "abandon_hypothesis",
        "handoff_ir",
        "hunt_status",
        "load_skill",
    }
)

# What a call to each tool asked, in the record's words. Any other tool reads as its
# own name, with the values it was asked about.
_ASKED = {
    "search_findings": "Searched alerts",
    "get_finding": "Read alert",
    "telemetry_search": "Searched telemetry",
    "splunk_search": "Searched Splunk",
    "lookup_indicators": "Looked up in threat intel",
    "recall_entity": "Recalled from memory",
    "validate_hypothesis": "Put up for a verdict",
    "conclude_hunt": "Asked to conclude the hunt",
    "expand": "Opened evidence in full",
    "record": "Asked to record a causal step",
    "finish": "Asked to finish the trace",
}

_SUBJECT_KEYS = (
    "spl_query",
    "query",
    "values",
    "entity_keys",
    "finding_id",
    "indicator",
    "value",
    "entity",
    "hostname",
    "ip_address",
    "sql",
    "q",
    "name",
    "reason",
)

_STATUS_WORD = {
    "proven": "proven",
    "disproven": "ruled out",
    "inconclusive": "left inconclusive",
    "parked": "set aside",
    "handed_off": "handed to incident response",
    "active": "back in play",
}

_STANCE_WORD = {"supports": "Supports", "weakens": "Goes against"}

_GAP_PROVENANCES = frozenset({"tool_failure", "deployment_gap", "operator_gap"})


class RecordContext:
    """What a kernel row names by id, read once off the run's own events.

    A link names an evidence id and an explanation id, a patch an id, a resolution a
    checkpoint id: the words for each are earlier on the same ledger.
    """

    def __init__(self, events: Sequence[Mapping[str, Any]] = ()) -> None:
        self.hypotheses: dict[str, str] = {}
        self.evidence: dict[str, str] = {}
        self.checkpoints: dict[str, str] = {}
        self.questions: dict[str, str] = {}
        for event in events:
            body = event.get("payload")
            if not isinstance(body, Mapping):
                continue
            kind = event.get("kind")
            if kind == "hypothesis" and body.get("hypothesis_id"):
                self.hypotheses[str(body["hypothesis_id"])] = str(
                    body.get("statement") or ""
                )
            elif kind == "evidence" and body.get("evidence_id"):
                self.evidence[str(body["evidence_id"])] = str(body.get("summary") or "")
            elif kind == "checkpoint" and body.get("checkpoint_id"):
                self.checkpoints[str(body["checkpoint_id"])] = str(
                    body.get("question") or ""
                )
            elif kind == "question" and body.get("question_id"):
                self.questions[str(body["question_id"])] = str(
                    body.get("question") or ""
                )

    def statement(self, hypothesis_id: Any) -> str:
        return self.hypotheses.get(str(hypothesis_id), "") or str(hypothesis_id or "")


def _args(text: Any) -> Mapping[str, Any]:
    if isinstance(text, Mapping):
        return text
    if not isinstance(text, str):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [one.strip() for one in value if isinstance(one, str) and one.strip()]
    return []


def _listed(values: Sequence[str], shown: int = 3) -> str:
    head = list(values[:shown])
    rest = len(values) - len(head)
    return ", ".join(head) + (f" and {rest} more" if rest > 0 else "")


def _quoted(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return f"“{flat if len(flat) <= limit else flat[: limit - 3] + '...'}”"


# "N row(s) from <source>" opens every rendered tool result.
_ROWS = re.compile(r"^(?:<vigil:tool_result[^>]*>\s*)?(\d+) rows?\b")
_BODY = re.compile(r"^<vigil:tool_result[^>]*>\n?(.*?)\n?</vigil:tool_result>$", re.S)


def _rows_in(result: str) -> Optional[int]:
    """Rows a call got back. A tool that wraps its answer ({success, query,
    results: [...]}, as the Splunk tools do) says one row, the wrapper."""
    said = _ROWS.match(result)
    if not said:
        return None
    count = int(said.group(1))
    if count != 1:
        return count
    found = _BODY.match(result)
    body = found.group(1) if found else result
    try:
        parsed = json.loads(body.split("\n", 1)[1])
    except (IndexError, ValueError):
        return count
    only = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else None
    if isinstance(only, Mapping) and isinstance(only.get("results"), list):
        return len(only["results"])
    return count


def _verb(tool: str) -> str:
    if tool in _ASKED:
        return _ASKED[tool]
    # An MCP tool is "<server>_<tool>": a Splunk search reads as one by any server name.
    if "splunk" in tool and ("execute" in tool or "search" in tool):
        return "Searched Splunk"
    return tool.replace("_", " ").replace("-", " ").capitalize()


def _failure(value: Any) -> str:
    """A refused or failed call's reason: ``{kind, detail}`` or the text as written."""
    if isinstance(value, Mapping):
        return _short(_join(value.get("kind"), value.get("detail")))
    return _short(value or "")


def _call_text(body: Mapping[str, Any], context: RecordContext) -> Optional[str]:
    tool = str(body.get("tool") or "")
    ok = body.get("ok") is not False
    if ok and tool in _SHOWN_ELSEWHERE:
        return None
    args = _args(body.get("arguments"))
    if tool == "validate_hypothesis":
        subject = [context.statement(args.get("hypothesis_id"))]
    elif tool == "expand":
        subject = [
            context.evidence.get(one, one) for one in _strings(args.get("evidence_ids"))
        ]
    else:
        subject = next(
            (found for key in _SUBJECT_KEYS if (found := _strings(args.get(key)))),
            [],
        )
    verb = _verb(tool)
    text = f"{verb}: {_listed(subject)}" if subject else verb
    if not ok:
        return _join(f"Refused — {text}", _failure(body.get("failure")))
    count = _rows_in(str(body.get("result") or ""))
    if count is not None and subject:
        return f"{text} → {count:,} {'row' if count == 1 else 'rows'}"
    return text


def _patch_text(body: Mapping[str, Any], context: RecordContext) -> Optional[str]:
    fields = body.get("fields")
    if not isinstance(fields, Mapping):
        return None
    target, ident = body.get("target"), body.get("id")
    if target == "hypothesis":
        status = fields.get("status")
        reason = fields.get("resolution_reason")
        if isinstance(status, str):
            word = _STATUS_WORD.get(status, status.replace("_", " "))
            return _join(
                f"Explanation {word}: {_quoted(context.statement(ident))}", reason
            )
        if reason:
            return _join(f"Explanation {_quoted(context.statement(ident))}", reason)
        return None
    if target == "question":
        status = fields.get("status")
        if status in ("closed", "parked"):
            word = "closed" if status == "closed" else "set aside"
            asked = context.questions.get(str(ident), str(ident or ""))
            return _join(f"Lead {word}: {asked}", fields.get("closed_reason"))
        return None
    if target == "hunt":
        status = fields.get("status")
        if status == "parked":
            return _join("Hunt paused", fields.get("parked_reason"))
        if status == "active" and "parked_reason" in fields:
            return "Hunt went on"
        if isinstance(fields.get("budgets"), Mapping):
            budgets = fields["budgets"]
            return _join(
                "Limits raised",
                f"{budgets.get('max_iterations')} turns, ${budgets.get('max_cost_usd')}",
            )
        if isinstance(fields.get("scope"), Mapping):
            return _join(
                "Scope widened", _listed(_strings(fields["scope"].get("entities")))
            )
    return None


def event_text(
    kind: str, payload: Any, context: Optional[RecordContext] = None
) -> Optional[str]:
    """One run row in words; None for a row the record does not print."""
    body = payload if isinstance(payload, Mapping) else {}
    known = context or RecordContext()
    if kind in _QUIET_KINDS:
        return None
    if kind == "decision":
        return _join(body.get("action"), body.get("rationale")) or kind
    if kind == "finding":
        return _join(body.get("agent_id"), _short(body.get("answer"))) or kind
    if kind == "dispatch":
        # A dispatch naming only who ran it repeats the call row beside it.
        if not body.get("query_intent") and not body.get("failure_reason"):
            return None
        return (
            _join(
                body.get("query_intent") or body.get("agent_id"),
                body.get("failure_reason"),
            )
            or kind
        )
    if kind == "terminal":
        return _join("Ended", body.get("outcome"), body.get("reason"))
    if kind == "resolution":
        asked = known.checkpoints.get(str(body.get("checkpoint_id")))
        if asked:
            answer = {"approve": "Approved", "reject": "Rejected"}.get(
                str(body.get("answer")), str(body.get("answer") or "Answered")
            )
            actor = f" by {body['actor']}" if body.get("actor") else ""
            return _join(f"{answer}{actor}: {asked}", body.get("text"))
        return _join(body.get("answer"), body.get("text")) or kind
    if kind == "directive":
        return _join(body.get("kind"), body.get("text")) or kind
    if kind == "recall":
        return "Opening recall"
    if kind == "run":
        return "Run started"
    if kind == "resumed":
        return _join("Picked back up", body.get("enqueued_by"))
    if kind == "call":
        return _call_text(body, known)
    if kind == "patch":
        return _patch_text(body, known)
    if kind == "evidence":
        summary = body.get("summary") or body.get("evidence_id")
        if body.get("provenance") in _GAP_PROVENANCES:
            return _join("Blind spot", summary)
        return str(summary or kind)
    if kind == "hypothesis":
        lead = (
            "Benign explanation to beat"
            if body.get("provenance") == "base_rate"
            else "Explanation put up"
        )
        return _join(lead, body.get("statement")) or kind
    if kind == "link":
        word = _STANCE_WORD.get(str(body.get("relation")))
        if word is None:
            # A "neither" ruling moves no standing; the evidence row already shows.
            return None
        evidence = known.evidence.get(str(body.get("evidence_id"))) or body.get(
            "evidence_id"
        )
        return _join(
            f"{word} {_quoted(known.statement(body.get('hypothesis_id')))}", evidence
        )
    if kind == "question":
        if body.get("status") in ("closed", "parked"):
            return _join("Lead closed", body.get("question"), body.get("closed_reason"))
        return _join("Lead opened", body.get("question")) or kind
    if kind == "checkpoint":
        return _join("Asked you", body.get("question")) or kind
    if kind == "handoff":
        return _join("Handed to incident response", body.get("title")) or kind
    if kind == "conclusion":
        value = body.get("value") if isinstance(body.get("value"), Mapping) else {}
        return _join("Concluded", _short(value.get("summary") or value.get("text")))
    if kind == "step":
        status = body.get("link_status") if body.get("cause_id") else None
        origin = (
            "origin, " + str(body.get("origin_status")) if body.get("origin") else None
        )
        via = f" via {body.get('link')}" if body.get("link") else ""
        tie = (
            f"caused by {body.get('cause_id')}{via} ({status})"
            if body.get("cause_id")
            else None
        )
        return _join(
            f"Step {body.get('step_id') or ''}".strip(),
            _short(body.get("event")),
            body.get("who"),
            tie,
            origin,
        )
    if kind == "notice":
        return _join("Notice", _short(body.get("text"))) or kind
    if kind == "phase":
        answer = body.get("answer") if isinstance(body.get("answer"), Mapping) else {}
        return _join(
            f"Phase {body.get('name') or body.get('phase_id') or ''} by {body.get('agent') or 'an agent'}",
            _short(answer.get("summary")),
        )
    if kind == "review":
        return (
            _join("Reviewed", body.get("stance"), _short(body.get("reasons"))) or kind
        )
    if kind == "narrative":
        return _join("Account written", _short(body.get("summary"))) or kind
    if kind == "child":
        return _join(body.get("role"), body.get("outcome"), body.get("reason")) or kind
    if kind == "unbound":
        return _join("No tool here answers", body.get("capability")) or kind
    return kind


def audit_text(row: CaseAuditLog) -> str:
    summary = (row.change_summary or "").strip()
    if summary:
        return summary
    change = f"{row.action} {row.field_name or ''}".strip()
    if row.old_value or row.new_value:
        return f"{change}: {row.old_value or '—'} → {row.new_value or '—'}"
    return change or row.action


def unread_row(run_id: str, at: Any, reason: str) -> dict:
    """One row standing in for a run whose ledger could not be read."""
    return {
        "id": f"run:{run_id}:unread",
        "at": _at(_parse(at)),
        "kind": "unread",
        "source": "run",
        "chained": False,
        "text": _join(f"Could not read the record of run {run_id}", reason),
        "run_id": run_id,
    }


def merge_record(
    runs: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    audits: Sequence[CaseAuditLog],
    activities: Sequence[Any] = (),
    unread: Sequence[Mapping[str, Any]] = (),
) -> list[dict]:
    """Every run's rows, case-audit rows and case-activity rows, newest first.

    ``runs`` is each run's id with its events. Run rows are the chained ledger,
    each read against its own run's ids. Audit and activity rows are not chained:
    the page says so. ``unread`` rows (``unread_row``) stand in for a run whose
    ledger could not be read. The kernel's bookkeeping (frames, prices, counters,
    "neither" rulings) is left out, and the chain itself is still verified whole.
    """
    rows: list[dict] = [{**row, "_seq": 0} for row in unread]
    for run_id, events in runs:
        context = RecordContext(events)
        for event in events:
            kind = str(event.get("kind") or "")
            seq = event.get("seq")
            text = event_text(kind, event.get("payload"), context)
            if text is None:
                continue
            rows.append(
                {
                    "id": f"run:{run_id}:{seq}",
                    "at": str(event.get("ts") or ""),
                    "kind": kind,
                    "source": "run",
                    "chained": True,
                    "text": text,
                    "run_id": run_id,
                    # A turn lands in one transaction, so every row shares `now()`.
                    # Seq is the order inside that instant.
                    "_seq": (
                        seq
                        if isinstance(seq, (int, float)) and not isinstance(seq, bool)
                        else 0
                    ),
                }
            )
    for audit in audits:
        rows.append(
            {
                "id": f"audit:{audit.audit_id}",
                "at": _at(audit.timestamp),
                "kind": "case_audit_logs",
                "source": "case_audit_logs",
                "chained": False,
                "text": audit_text(audit),
                "_seq": 0,
            }
        )
    # Activities have no id; the list is only appended to, so the index is stable.
    for index, activity in enumerate(activities):
        if not isinstance(activity, Mapping):
            continue
        at = _parse(activity.get("timestamp"))
        if at is None:
            continue
        kind = str(activity.get("activity_type") or "")
        rows.append(
            {
                "id": f"activity:{index}",
                "at": _at(at),
                "kind": kind,
                "source": "case_activities",
                "chained": False,
                "text": _plain(activity.get("description")) or kind,
                "_seq": 0,
            }
        )
    rows.sort(key=lambda row: (_parse(row["at"]) or _NEVER, row["_seq"]), reverse=True)
    for row in rows:
        row.pop("_seq", None)
    return rows
