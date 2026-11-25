"""Day 1 Report history sections: the customer's own before, the re-check list,
and tuning candidates.

Computed from ``history_imports`` and ``history_tickets`` over one window: the
latest 90 days of closures per ticket source, ending at that source's latest
close. The record stores figures, counts, rule ids and ticket references. It
does not store a closer, notes, a title, an entity value, or an unmapped
disposition string. Those are read back in :func:`present` for the reader in
front of the page.
"""

from __future__ import annotations

import ipaddress
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth.permissions import missing_permission
from core.detections.inventory.accurate import FAIL_BELOW, MIN_JUDGED
from core.memory.imported_history import list_imports
from core.reporting.analytics_service import _wilson
from core.reporting.checks import HISTORY
from core.storage.ip_address import normalize_ip
from core.storage.models import HistoryImport, HistoryTicket
from core.storage.unit_of_work import unit_of_work

WINDOW = timedelta(days=90)
LATER = timedelta(days=30)
TOP = 50
THIN = 10
FAST = 0.1
ENTITY_TYPES = ("host", "user", "ip")
# A connected pull of these is the customer's EDR. Anything else is the SIEM.
EDR = frozenset(
    {
        "microsoft_defender",
        "defender",
        "sentinelone",
        "crowdstrike",
        "carbon_black",
        "carbonblack",
    }
)
CANDIDATE = frozenset({"benign", "false_positive", "unable_to_resolve"})
SEVERITY = ("critical", "high", "medium", "low", "info", "unknown")
_API = {
    "history.before": "history_before",
    "history.recheck": "history_recheck",
    "history.tuning": "history_tuning",
}
_LEFT_KIND = {"tickets": "Tickets", "alerts": "Alerts", "rows": "Rows"}
_LEFT_REASON = {
    "duplicate": "duplicate",
    "no_closed_at": "no close time",
    "unmapped_disposition": "unmapped disposition",
    "bulk_close": "bulk-closed",
    "auto_closed": "auto-closed",
    "no_timestamp": "no timestamp",
    "no_entities": "no entities",
    "unreadable": "unreadable",
}
_DISPOSITION = {
    "resolved": "true positive",
    "benign": "benign",
    "false_positive": "false positive",
    "unable_to_resolve": "undetermined",
}
_HOW_ADD = (
    "Add history from the history card in Settings or during setup, "
    "then run the Day 1 Report again."
)
_HOW_RERUN = (
    "Re-run it from the history card in Settings or during setup, "
    "then run the Day 1 Report again."
)
_HOW_WAIT = "When it finishes, run the Day 1 Report again."
_READ_FAILED = "not checked: could not read imported history."
_TUNING_DEFINITION = (
    "true positives among judged closures; "
    "judged excludes bulk-closed and undetermined"
)


def produce() -> dict[str, Any]:
    """The three sections, keyed by module id. The caller stores them on the record."""
    with unit_of_work() as session:
        return _produce(session)


def present(
    session: Session,
    evidence: dict[str, Any],
    modules: list[str],
    hidden: set[str],
    recorded_at: datetime,
    record_id: str,
    run_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Reader-specific sections. Withheld modules are left out.

    Titles, entity values and unmapped names are filled only when this reader
    holds ``automation.read`` and the import still has the rows the run read.
    A public address links to the exclusion form only for ``findings.write``.
    """
    out: dict[str, Any] = {name: None for name in _API.values()}
    text = missing_permission(user_id, "automation.read", session) is None
    write = missing_permission(user_id, "findings.write", session) is None
    for module in HISTORY:
        if module.id not in modules or module.id in hidden:
            continue
        stored = evidence.get(module.id) or {"not_checked": _READ_FAILED}
        out[_API[module.id]] = _present_section(
            session, module.id, stored, recorded_at, record_id, run_id, text, write
        )
    return out


def _produce(session: Session) -> dict[str, Any]:
    rows = list_imports(session)
    gated = _gate(rows)
    if gated is not None:
        return {module.id: gated for module in HISTORY}
    imports = [_stored_import(row) for row in rows]
    notes = _shared_notes(rows)
    included = {
        row["import_id"]: row
        for row in rows
        if row["status"] == "done" and row["has_ticket_detail"]
    }
    chosen = _chosen(session, included)
    by_source: dict[str, list[tuple[HistoryTicket, dict]]] = defaultdict(list)
    for ticket, imp, source in chosen:
        by_source[source].append((ticket, imp))
    slices = []
    windows = []
    for source, group in sorted(by_source.items()):
        start, end, kept, outside = _window(group)
        if not kept:
            continue
        slices.append((source, start, end, outside, kept))
        windows.append(
            {
                "source": source,
                "start": _iso(start),
                "end": _iso(end),
                "outside": outside,
            }
        )
    _overlap_note(notes, slices)
    truths = _true_positives(chosen)
    shell = {"notes": notes, "imports": imports, "windows": windows}
    return {
        "history.before": {
            "sources": [_source_body(*item) for item in slices],
            **shell,
        },
        "history.recheck": {
            **shell,
            "notes": notes + _recheck_notes(slices),
            **_recheck(slices, truths),
        },
        "history.tuning": {**shell, "candidates": _tuning(slices, truths)},
    }


def _gate(rows: list[dict]) -> Optional[dict]:
    """No figures while an import runs, or when nothing finished."""
    running = [row for row in rows if row["status"] == "running"]
    if running:
        since = min(_aware(row["started_at"]) for row in running)
        return _not_checked(
            f"not checked: import running since {_stamp(since)}", _HOW_WAIT, rows
        )
    done = [row for row in rows if row["status"] == "done"]
    if done:
        return None
    stopped = [row for row in rows if row["status"] in ("failed", "incomplete")]
    if not rows:
        return _not_checked("not checked: no history imported", _HOW_ADD, rows)
    if stopped:
        moment = _aware(stopped[0]["finished_at"] or stopped[0]["started_at"])
        return _not_checked(
            f"not checked: the import on {_day(moment)} didn't complete; re-run it",
            _HOW_RERUN,
            rows,
        )
    return _not_checked("not checked: no history imported", _HOW_ADD, rows)


def _not_checked(reason: str, how: str, rows: list[dict]) -> dict[str, Any]:
    return {
        "not_checked": reason,
        "how": how,
        "notes": [],
        "imports": [_stored_import(row) for row in rows],
        "windows": [],
    }


def _stored_import(row: dict) -> dict[str, Any]:
    included = row["status"] == "done" and bool(row["has_ticket_detail"])
    note = None
    if row["status"] in ("failed", "incomplete"):
        moment = _aware(row["finished_at"] or row["started_at"])
        note = f"The import on {_day(moment)} didn't complete and is not included."
    elif row["status"] == "running":
        note = f"Import running since {_stamp(_aware(row['started_at']))}."
    elif row["status"] == "done" and not row["has_ticket_detail"]:
        moment = _aware(row["finished_at"] or row["started_at"])
        note = (
            f"The import on {_day(moment)} predates ticket detail; "
            "importing again adds it."
        )
    unmapped = row.get("unmapped") or []
    count = sum(int(item.get("count") or 0) for item in unmapped)
    tested = int(row.get("bulk_not_tested") or 0)
    return {
        "id": row["import_id"],
        "source": row["source_system"] or "file",
        "imported_at": _iso(_aware(row["finished_at"] or row["started_at"])),
        "has_ticket_detail": bool(row["has_ticket_detail"]),
        "included": included,
        "note": note,
        "left_out": _left_out(row.get("left_out") or {}),
        "unmapped_count": count,
        "bulk_not_tested": tested or None,
    }


def _left_out(raw: dict) -> list[str]:
    lines = []
    for kind, reasons in raw.items():
        if not isinstance(reasons, dict):
            continue
        parts = []
        for reason, count in reasons.items():
            if not count:
                continue
            word = _LEFT_REASON.get(str(reason), str(reason).replace("_", " "))
            parts.append(f"{count} {word}")
        if parts:
            lines.append(f"{_LEFT_KIND.get(str(kind), str(kind))}: {', '.join(parts)}")
    return lines


def _shared_notes(rows: list[dict]) -> list[str]:
    notes = []
    for row in rows:
        if row["status"] in ("failed", "incomplete"):
            moment = _aware(row["finished_at"] or row["started_at"])
            notes.append(
                f"The import on {_day(moment)} didn't complete and is not included."
            )
        elif row["status"] == "done" and not row["has_ticket_detail"]:
            moment = _aware(row["finished_at"] or row["started_at"])
            notes.append(
                f"The import on {_day(moment)} predates ticket detail; "
                "importing again adds it."
            )
    return notes


def _chosen(
    session: Session, included: dict[str, dict]
) -> list[tuple[HistoryTicket, dict, str]]:
    """One row per source and ticket id. A missing id is never merged."""
    if not included:
        return []
    tickets = session.scalars(
        select(HistoryTicket).where(HistoryTicket.import_id.in_(list(included)))
    ).all()
    grouped: dict[tuple[str, str], list[tuple[HistoryTicket, dict]]] = defaultdict(list)
    loose: list[tuple[HistoryTicket, dict, str]] = []
    for ticket in tickets:
        imp = included[ticket.import_id]
        source = (
            ticket.source_system or imp["source_system"] or "file"
        ).strip() or "file"
        if ticket.ticket_id:
            grouped[(source, ticket.ticket_id)].append((ticket, imp))
        else:
            loose.append((ticket, imp, source))
    chosen: list[tuple[HistoryTicket, dict, str]] = []
    for (source, _ticket_id), group in grouped.items():
        ticket, imp = max(group, key=_later)
        chosen.append((ticket, imp, source))
    chosen.extend(loose)
    return chosen


def _later(pair: tuple[HistoryTicket, dict]) -> tuple:
    ticket, imp = pair
    moment = _aware(imp["finished_at"] or imp["started_at"])
    return (moment, ticket.position)


def _window(
    group: list[tuple[HistoryTicket, dict]],
) -> tuple[datetime, datetime, list[tuple[HistoryTicket, dict]], int]:
    latest = max(_aware(ticket.closed_at) for ticket, _imp in group)
    start = latest - WINDOW
    kept = [(ticket, imp) for ticket, imp in group if _aware(ticket.closed_at) >= start]
    return start, latest, kept, len(group) - len(kept)


def _overlap_note(notes: list[str], slices: list[tuple]) -> None:
    for _source, _start, _end, _outside, group in slices:
        spans: dict[str, list[datetime]] = defaultdict(list)
        missing = False
        for ticket, imp in group:
            spans[imp["import_id"]].append(_aware(ticket.closed_at))
            if not ticket.ticket_id:
                missing = True
        ids = list(spans)
        if not missing or len(ids) < 2:
            continue
        overlapped = False
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                if min(spans[left]) <= max(spans[right]) and min(spans[right]) <= max(
                    spans[left]
                ):
                    overlapped = True
        if overlapped:
            notes.append("Tickets may be counted twice.")
            return


def _source_body(
    source: str,
    start: datetime,
    end: datetime,
    outside: int,
    group: list[tuple[HistoryTicket, dict]],
) -> dict[str, Any]:
    return {
        "source": source,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "outside": outside,
        **_metrics(group, start, end),
        "by_severity": _by_severity(group, start, end),
    }


def _by_severity(
    group: list[tuple[HistoryTicket, dict]], start: datetime, end: datetime
) -> list[dict]:
    rows = []
    for level in SEVERITY:
        subset = [
            (ticket, imp)
            for ticket, imp in group
            if (ticket.severity or "unknown") == level
        ]
        if subset:
            rows.append({"severity": level, **_metrics(subset, start, end)})
    return rows


def _metrics(
    group: list[tuple[HistoryTicket, dict]], start: datetime, end: datetime
) -> dict[str, Any]:
    tickets = [ticket for ticket, _imp in group]
    return {
        "closures": len(tickets),
        "dispositions": _dispositions(tickets, start, end),
        "time_to_close": _times(group, start, end),
        "bulk": _bulk(group, start, end),
        "per_day": _per_day(group, start, end),
    }


def _dispositions(
    tickets: list[HistoryTicket], start: datetime, end: datetime
) -> list[dict]:
    counts = Counter(ticket.category for ticket in tickets)
    n = len(tickets)
    benign = counts.get("benign", 0)
    false_positive = counts.get("false_positive", 0)
    figures = []
    if benign and false_positive:
        figures.append(
            _figure("benign", benign, n, "share of closures in the window", start, end)
        )
        figures.append(
            _figure(
                "false positive",
                false_positive,
                n,
                "share of closures in the window",
                start,
                end,
            )
        )
    elif benign or false_positive:
        figures.append(
            _figure(
                "benign or false positive, not told apart",
                benign + false_positive,
                n,
                "share of closures in the window",
                start,
                end,
            )
        )
    for category, label in (
        ("resolved", "true positive"),
        ("unable_to_resolve", "undetermined"),
    ):
        if counts.get(category):
            figures.append(
                _figure(
                    label,
                    counts[category],
                    n,
                    "share of closures in the window",
                    start,
                    end,
                )
            )
    return figures


def _figure(
    label: str, k: int, n: int, definition: str, start: datetime, end: datetime
) -> dict[str, Any]:
    low = high = None
    kind = "measured"
    if n >= MIN_JUDGED:
        span = _wilson(k, n)
        if span is not None:
            low, high = span
            kind = "estimated"
    return {
        "label": label,
        "k": k,
        "n": n,
        "kind": kind,
        "definition": definition,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "low": low,
        "high": high,
    }


def _times(
    group: list[tuple[HistoryTicket, dict]], start: datetime, end: datetime
) -> list[dict]:
    buckets: dict[tuple[str, str], list[tuple[HistoryTicket, dict]]] = defaultdict(list)
    for ticket, imp in group:
        buckets[_time_definition(ticket, imp, start, end)].append((ticket, imp))
    figures = []
    for (definition, kind), bucket in buckets.items():
        missing = sum(
            1
            for ticket, _imp in bucket
            if ticket.opened_missing or ticket.opened_at is None
        )
        measured = []
        for ticket, _imp in bucket:
            if ticket.opened_missing or ticket.opened_at is None:
                continue
            measured.append(
                (_aware(ticket.closed_at) - _aware(ticket.opened_at)).total_seconds()
            )
        figure: dict[str, Any] = {
            "definition": definition,
            "kind": kind,
            "window_start": _iso(start),
            "window_end": _iso(end),
            "count": len(measured),
            "opened_missing": missing,
            "median": None,
            "p90": None,
            "bands": None,
        }
        if len(bucket) >= MIN_JUDGED and measured:
            figure["median"] = _duration(_percentile(measured, 0.5))
            figure["p90"] = _duration(_percentile(measured, 0.9))
        elif measured:
            bands = Counter(_band(seconds) for seconds in measured)
            figure["bands"] = [
                {"label": label, "count": bands.get(label, 0)}
                for label in ("under an hour", "a day", "a week", "longer")
                if bands.get(label)
            ]
        figures.append(figure)
    return figures


def _time_definition(
    ticket: HistoryTicket, imp: dict, start: datetime, end: datetime
) -> tuple[str, str]:
    if imp.get("pull_days"):
        pulled = _aware(
            imp.get("pulled_at") or imp.get("finished_at") or imp["started_at"]
        )
        text = (
            f"alerts created in {_day(start)} to {_day(end)} "
            f"and closed by {_stamp(pulled)}"
        )
        return text, "estimated"
    if ticket.closed_approximate:
        return "incident created to last modified, approximate", "estimated"
    source = (ticket.source_system or "").strip()
    if source == "microsoft_defender":
        return "created to resolved", "measured"
    return "opened to closed, as exported", "measured"


def _band(seconds: float) -> str:
    if seconds < 3600:
        return "under an hour"
    if seconds < 86400:
        return "a day"
    if seconds < 7 * 86400:
        return "a week"
    return "longer"


def _bulk(
    group: list[tuple[HistoryTicket, dict]], start: datetime, end: datetime
) -> dict[str, Any]:
    tickets = [ticket for ticket, _imp in group]
    k = sum(1 for ticket in tickets if ticket.bulk_closed)
    figure = _figure(
        "bulk-closed",
        k,
        len(tickets),
        "bulk-closed closures in the window",
        start,
        end,
    )
    phrases = []
    seen = set()
    for _ticket, imp in group:
        tested = int(imp.get("bulk_not_tested") or 0)
        if not tested or imp["import_id"] in seen:
            continue
        seen.add(imp["import_id"])
        moment = _aware(imp["finished_at"] or imp["started_at"])
        phrases.append(
            f"{imp.get('source_system') or 'file'} import on {_day(moment)}: "
            f"{tested} not tested, as the import recorded it"
        )
    figure["not_tested"] = phrases
    return figure


def _per_day(
    group: list[tuple[HistoryTicket, dict]], start: datetime, end: datetime
) -> dict[str, Any]:
    closes = [_aware(ticket.closed_at) for ticket, _imp in group]
    span_days = 0.0
    per_day = None
    if closes:
        span_days = (max(closes) - min(closes)).total_seconds() / 86400
        if span_days > 0:
            per_day = round(len(closes) / span_days, 2)
    estimated = any(
        ticket.closed_approximate or imp.get("pull_days") for ticket, imp in group
    )
    return {
        "count": len(closes),
        "span_days": round(span_days, 4),
        "per_day": per_day,
        "kind": "estimated" if estimated else "measured",
        "definition": "closures from the earliest close in the window to the latest",
        "window_start": _iso(start),
        "window_end": _iso(end),
    }


def _recheck_notes(slices: list[tuple]) -> list[str]:
    notes = []
    defender = False
    few = []
    for source, _start, _end, _outside, group in slices:
        candidates = [ticket for ticket, _imp in group if ticket.category in CANDIDATE]
        if any(ticket.source_system == "microsoft_defender" for ticket in candidates):
            defender = True
        if candidates and len(group) < MIN_JUDGED:
            few.append(source)
    if defender:
        notes.append("not checked: Defender gives a category, not notes")
    for source in few:
        notes.append(f"{source}: too few to rank")
    return notes


def _recheck(
    slices: list[tuple], truths: list[tuple[datetime, set[str], str, int]]
) -> dict[str, Any]:
    entries = []
    medians = _source_medians(slices)
    fingerprints = Counter(
        (source, ticket.notes_fingerprint)
        for source, _start, _end, _outside, group in slices
        for ticket, _imp in group
        if ticket.notes_fingerprint
    )
    for source, _start, _end, _outside, group in slices:
        for ticket, _imp in group:
            if ticket.category not in CANDIDATE:
                continue
            reasons = _reasons(ticket, source, medians, fingerprints, truths)
            entries.append((ticket, source, reasons))
    entries.sort(key=_rank)
    stored = [
        {
            "ref": _ref(ticket),
            "source": source,
            "closed_at": _iso(_aware(ticket.closed_at)),
            "disposition": _DISPOSITION.get(ticket.category, ticket.category),
            "severity": ticket.severity or "unknown",
            "reasons": reasons,
        }
        for ticket, source, reasons in entries[:TOP]
    ]
    return {"total": len(entries), "entries": stored}


def _reasons(
    ticket: HistoryTicket,
    source: str,
    medians: dict[str, Optional[float]],
    fingerprints: Counter,
    truths: list[tuple[datetime, set[str], str, int]],
) -> list[str]:
    reasons = []
    if ticket.bulk_closed:
        reasons.append("bulk_closed")
    median = medians.get(source)
    if (
        median is not None
        and not ticket.opened_missing
        and ticket.opened_at is not None
    ):
        duration = (_aware(ticket.closed_at) - _aware(ticket.opened_at)).total_seconds()
        if duration < median * FAST:
            reasons.append("closed_fast")
    if ticket.source_system != "microsoft_defender":
        shared = fingerprints.get((source, ticket.notes_fingerprint), 0)
        if ticket.notes_empty or (ticket.notes_fingerprint and shared >= THIN):
            reasons.append("thin_notes")
    if _tied(ticket, truths):
        reasons.append("later_true_positive")
    if ticket.category == "unable_to_resolve":
        reasons.append("undetermined")
    return reasons


def _source_medians(slices: list[tuple]) -> dict[str, Optional[float]]:
    medians: dict[str, Optional[float]] = {}
    for source, _start, _end, _outside, group in slices:
        if len(group) < MIN_JUDGED:
            medians[source] = None
            continue
        measured = []
        for ticket, _imp in group:
            if ticket.opened_missing or ticket.opened_at is None:
                continue
            measured.append(
                (_aware(ticket.closed_at) - _aware(ticket.opened_at)).total_seconds()
            )
        medians[source] = _percentile(measured, 0.5) if measured else None
    return medians


def _true_positives(
    chosen: list[tuple[HistoryTicket, dict, str]],
) -> list[tuple[datetime, set[str], str, int]]:
    found = []
    for ticket, _imp, _source in chosen:
        if ticket.category != "resolved":
            continue
        found.append(
            (
                _aware(ticket.closed_at),
                set(_interesting(ticket.entity_keys)),
                ticket.import_id,
                ticket.position,
            )
        )
    return found


def _tied(
    ticket: HistoryTicket, truths: list[tuple[datetime, set[str], str, int]]
) -> bool:
    return any(
        _tied_key(ticket, key, truths) for key in _interesting(ticket.entity_keys)
    )


def _tied_key(
    ticket: HistoryTicket,
    key: str,
    truths: list[tuple[datetime, set[str], str, int]],
) -> bool:
    opened = _aware(ticket.closed_at)
    for closed, other, import_id, position in truths:
        if (import_id, position) == (ticket.import_id, ticket.position):
            continue
        if opened < closed <= opened + LATER and key in other:
            return True
    return False


def _interesting(keys: Optional[list[str]]) -> list[str]:
    found = []
    for key in keys or []:
        kind, sep, value = str(key).partition(":")
        if sep and kind in ENTITY_TYPES and value:
            found.append(str(key))
    return found


def _rank(item: tuple[HistoryTicket, str, list[str]]) -> tuple:
    ticket, _source, reasons = item
    severity = (
        SEVERITY.index(ticket.severity)
        if ticket.severity in SEVERITY
        else len(SEVERITY)
    )
    return (-len(reasons), severity, -_aware(ticket.closed_at).timestamp())


def _tuning(
    slices: list[tuple], truths: list[tuple[datetime, set[str], str, int]]
) -> list[dict]:
    rules: dict[tuple, list[tuple[HistoryTicket, dict]]] = defaultdict(list)
    entities: dict[tuple, list[tuple[HistoryTicket, dict]]] = defaultdict(list)
    for source, start, end, _outside, group in slices:
        for ticket, imp in group:
            if ticket.bulk_closed or ticket.category == "unable_to_resolve":
                continue
            pair = (ticket, imp)
            if ticket.rule_id:
                rules[(source, "rule", ticket.rule_id, start, end)].append(pair)
            else:
                # Grouped by the title in memory. The record keeps a reference.
                rules[(source, "title", ticket.title or "", start, end)].append(pair)
            for key in _interesting(ticket.entity_keys):
                entities[(source, key, start, end)].append(pair)
    candidates = []
    for (source, group, _label, start, end), pairs in rules.items():
        if any(_tied(ticket, truths) for ticket, _imp in pairs):
            continue
        stored = _candidate(pairs, source, start, end)
        if stored is None:
            continue
        ticket, _imp = pairs[0]
        stored["group"] = group
        stored["rule_id"] = ticket.rule_id if group == "rule" else None
        stored["ref"] = _ref(ticket)
        stored["entity_type"] = None
        stored["entity_index"] = None
        candidates.append(stored)
    for (source, key, start, end), pairs in entities.items():
        if any(_tied_key(ticket, key, truths) for ticket, _imp in pairs):
            continue
        stored = _candidate(pairs, source, start, end)
        if stored is None:
            continue
        ticket, _imp = pairs[0]
        keys = list(ticket.entity_keys or [])
        try:
            index = [str(item) for item in keys].index(key)
        except ValueError:
            continue
        kind, _sep, _value = key.partition(":")
        stored["group"] = "entity"
        stored["rule_id"] = None
        stored["ref"] = _ref(ticket)
        stored["entity_type"] = kind
        stored["entity_index"] = index
        candidates.append(stored)
    candidates.sort(
        key=lambda row: (row["true_positives"] / row["judged"], -row["judged"])
    )
    return candidates


def _candidate(
    pairs: list[tuple[HistoryTicket, dict]],
    source: str,
    start: datetime,
    end: datetime,
) -> Optional[dict]:
    judged = len(pairs)
    if judged < MIN_JUDGED:
        return None
    true_positives = sum(1 for ticket, _imp in pairs if ticket.category == "resolved")
    if true_positives / judged >= FAIL_BELOW:
        return None
    span = _wilson(true_positives, judged)
    low, high = span if span is not None else (None, None)
    return {
        "source": source,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "judged": judged,
        "true_positives": true_positives,
        "low": low,
        "high": high,
        "kind": "estimated",
        "definition": _TUNING_DEFINITION,
        "where": (
            "the customer's EDR" if source.lower() in EDR else "the customer's SIEM"
        ),
    }


def _ref(ticket: HistoryTicket) -> dict[str, Any]:
    return {
        "import_id": ticket.import_id,
        "position": ticket.position,
        "ticket_id": ticket.ticket_id,
    }


def _present_section(
    session: Session,
    module_id: str,
    stored: dict,
    recorded_at: datetime,
    record_id: str,
    run_id: str,
    text: bool,
    write: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "not_checked": stored.get("not_checked"),
        "how": stored.get("how"),
        "notes": list(stored.get("notes") or []),
        "imports": [
            _present_import(session, row, recorded_at)
            for row in stored.get("imports") or []
        ],
        "windows": list(stored.get("windows") or []),
    }
    if module_id == "history.before":
        body["sources"] = list(stored.get("sources") or [])
    elif module_id == "history.recheck":
        body["total"] = stored.get("total")
        body["entries"] = [
            _present_entry(session, row, recorded_at, text)
            for row in stored.get("entries") or []
        ]
    elif module_id == "history.tuning":
        body["candidates"] = [
            _present_candidate(
                session, row, recorded_at, record_id, run_id, text, write
            )
            for row in stored.get("candidates") or []
        ]
    return body


def _present_import(session: Session, stored: dict, recorded_at: datetime) -> dict:
    shown = {
        "id": stored.get("id"),
        "source": stored.get("source"),
        "imported_at": stored.get("imported_at"),
        "has_ticket_detail": bool(stored.get("has_ticket_detail")),
        "included": bool(stored.get("included")),
        "note": stored.get("note"),
        "left_out": list(stored.get("left_out") or []),
        "unmapped": [],
        "unmapped_note": None,
        "unmapped_count": int(stored.get("unmapped_count") or 0),
        "bulk_not_tested": stored.get("bulk_not_tested"),
    }
    if not shown["unmapped_count"]:
        return shown
    ticket, note = _row(
        session, {"import_id": stored.get("id"), "position": None}, recorded_at
    )
    if note or ticket is None:
        # ``_row`` with no position only checks the import. A missing import or a
        # later finish withholds the names. The count stays.
        shown["unmapped_note"] = note or "removed with its import"
        return shown
    ranked = Counter(ticket.unmapped or {}).most_common()
    names = [f"{value} ({count})" for value, count in ranked[:20]]
    more = len(ranked) - 20
    if more > 0:
        names.append(f"and {more} more")
    shown["unmapped"] = names
    return shown


def _present_entry(
    session: Session, stored: dict, recorded_at: datetime, text: bool
) -> dict:
    ref = stored.get("ref") or {}
    ticket_id = ref.get("ticket_id")
    entry = {
        "ticket": ticket_id or "no ticket ID in the export",
        "source": stored.get("source"),
        "closed_at": stored.get("closed_at"),
        "disposition": stored.get("disposition"),
        "severity": stored.get("severity"),
        "reasons": list(stored.get("reasons") or []),
        "title": None,
        "text_note": None,
    }
    if ticket_id:
        return entry
    row, note = _row(session, ref, recorded_at)
    if not text:
        entry["text_note"] = "Those fields need automation.read."
        return entry
    if note or row is None:
        entry["text_note"] = note or "removed with its import"
        return entry
    title = (row.title or "").strip()
    entry["title"] = title or None
    return entry


def _present_candidate(
    session: Session,
    stored: dict,
    recorded_at: datetime,
    record_id: str,
    run_id: str,
    text: bool,
    write: bool,
) -> dict:
    ref = stored.get("ref") or {}
    group = stored.get("group")
    shown = {
        "group": group,
        "rule_id": stored.get("rule_id"),
        "ticket": ref.get("ticket_id")
        or ("no ticket ID in the export" if group != "rule" else None),
        "label": None,
        "entity_type": stored.get("entity_type"),
        "text_note": None,
        "source": stored.get("source"),
        "window_start": stored.get("window_start"),
        "window_end": stored.get("window_end"),
        "judged": stored.get("judged"),
        "true_positives": stored.get("true_positives"),
        "low": stored.get("low"),
        "high": stored.get("high"),
        "kind": stored.get("kind") or "estimated",
        "definition": stored.get("definition") or _TUNING_DEFINITION,
        "where": stored.get("where"),
        "exclude_link": None,
        "exclude_note": None,
    }
    if group == "rule":
        return shown
    row, note = _row(session, ref, recorded_at)
    if not text:
        shown["text_note"] = "Those fields need automation.read."
        return shown
    if note or row is None:
        shown["text_note"] = note or "removed with its import"
        return shown
    if group == "title":
        shown["label"] = (row.title or "").strip() or None
        return shown
    value = _entity_value(row, stored.get("entity_type"), stored.get("entity_index"))
    if value is None:
        shown["text_note"] = "removed with its import"
        return shown
    shown["label"] = value
    _exclusion(shown, value, record_id, run_id, write)
    return shown


def _entity_value(
    ticket: HistoryTicket, entity_type: Optional[str], index: Any
) -> Optional[str]:
    keys = list(ticket.entity_keys or [])
    if not isinstance(index, int) or index < 0 or index >= len(keys):
        return None
    kind, sep, value = str(keys[index]).partition(":")
    if not sep or kind != entity_type or not value:
        return None
    return value


def _exclusion(
    shown: dict, value: str, record_id: str, run_id: str, write: bool
) -> None:
    addr = normalize_ip(value)
    if addr is None:
        return
    try:
        global_addr = ipaddress.ip_address(addr).is_global
    except ValueError:
        return
    if not global_addr:
        return
    if not write:
        shown["exclude_note"] = "excluding it needs the findings write permission"
        return
    query = urlencode({"exclude": addr, "report": record_id, "run": run_id})
    shown["exclude_link"] = f"/dashboard?{query}"


def _row(
    session: Session, ref: dict, recorded_at: datetime
) -> tuple[Optional[Any], Optional[str]]:
    """The import, or a history ticket when ``position`` is an int.

    A later ``finished_at`` is a re-derive: the entry keeps its figures and
    reads as of that import, with no text from the new rows.
    """
    import_id = ref.get("import_id")
    if not import_id:
        return None, "removed with its import"
    record = session.get(HistoryImport, import_id)
    if record is None:
        return None, "removed with its import"
    finished = _aware(record.finished_at) if record.finished_at else None
    if finished is None or finished > _aware(recorded_at):
        moment = finished or _aware(record.started_at)
        return None, f"as of the import on {_day(moment)}"
    position = ref.get("position")
    if not isinstance(position, int):
        return record, None
    ticket = session.scalar(
        select(HistoryTicket).where(
            HistoryTicket.import_id == import_id,
            HistoryTicket.position == position,
        )
    )
    if ticket is None:
        return None, "removed with its import"
    return ticket, None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - (rank - lower)) + ordered[upper] * (rank - lower)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(round(seconds))} s"
    if seconds < 3600:
        minutes = int(round(seconds / 60))
        return f"{minutes} min"
    if seconds < 86400:
        hours = seconds / 3600
        whole = int(hours)
        minutes = int(round((hours - whole) * 60))
        if minutes == 60:
            whole += 1
            minutes = 0
        return f"{whole} h" if minutes == 0 else f"{whole} h {minutes} min"
    days = seconds / 86400
    whole = int(days)
    hours = int(round((days - whole) * 24))
    if hours == 24:
        whole += 1
        hours = 0
    return f"{whole} d" if hours == 0 else f"{whole} d {hours} h"


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment: datetime) -> str:
    return _aware(moment).astimezone(timezone.utc).isoformat()


def _stamp(moment: datetime) -> str:
    shown = _aware(moment).astimezone(timezone.utc)
    return f"{shown.day} {shown:%b} {shown:%H:%M} UTC"


def _day(moment: datetime) -> str:
    shown = _aware(moment).astimezone(timezone.utc)
    return f"{shown.day} {shown:%b} {shown.year}"
