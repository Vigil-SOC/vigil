"""Imported history (#3965): a customer's pre-install alerts and closed tickets.

A closed ticket becomes a Verdict and an alert becomes a Sighting, under
``investigation_kind = 'imported'`` keyed ``<source system>:<content hash>``.
One ticket is one Verdict: nothing is aggregated into counts, and nothing here
writes a finding, a Case or a closure (#3961 decisions 6 and 7).

The ticket filters and raw-disposition values are #3890's
(``core/coldstart/tickets.py`` at ``d34602875884``), unchanged except that a raw
value maps straight to a ``ClosureCategory`` and then through
``CLOSURE_OUTCOMES``, so ``undetermined`` becomes ``unable_to_resolve`` rather
than a drop.

The import's own record is ``history_imports`` and its kept tickets, bulk-closed
included, are ``history_tickets`` (#4886); those hold severity, rule ID and the
raw disposition, which memory's rows have no place for. No closer is stored:
``closed_by``, assignee and owner are read only to find bulk closes.

A file's alerts are also ``history_alerts``, one row per alert id (#5091).
Each kept ticket stores the alert ids it named (#5092). Sightings stay
aggregated. A connected pull passes no alerts, so it writes none.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.auth.permissions import LEDGERS_ERASE
from core.cases.closure import CLOSURE_OUTCOMES, ClosureCategory
from core.memory.distil import clear_investigation
from core.memory.entity_keys import entity_context_candidates, entity_key
from core.memory.ledger_erasure import marked_run_ids
from core.memory.recall_contract import (
    ENTITY_KEY_TYPES,
    VERDICT_SUBJECT_CAP,
    InvestigationKind,
    Stance,
    Trust,
    WindowSource,
    bounded_prose,
)
from core.memory.source_tier import InvestigationKind as TierKind
from core.memory.source_tier import resolve_source_tier
from core.storage.connection import get_db_manager
from core.storage.models import (
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
    HistoryAlert,
    HistoryImport,
    HistoryTicket,
)
from core.storage.unit_of_work import unit_of_work

KIND = InvestigationKind.IMPORTED
IMPORT_WRITTEN = "memory.import.written"
IMPORT_DELETED = "memory.import.deleted"

# Raw values, lower-cased with runs of other characters folded to "_". A value not
# listed is reported as unmapped, never guessed (#3890's RAW_LABELS).
RAW_LABELS: Mapping[str, ClosureCategory] = {
    **dict.fromkeys(
        (
            "tp",
            "true_positive",
            "truepositive",
            "true_positive_confirmed",
            "true_positive_security_incident",
            "tp_incident",
            "confirmed",
            "confirmed_malicious",
        ),
        ClosureCategory.RESOLVED,
    ),
    **dict.fromkeys(
        (
            "bp",
            "benign",
            "benign_positive",
            "benignpositive",
            "benign_expected_activity",
            "expected",
            "expected_activity",
            "authorized_activity",
            "authorized_change",
            "authorized_change_ticket",
            # Defender for Endpoint's benign classification (#3981).
            "informationalexpectedactivity",
            "informational_expected_activity",
            "fp",
            "false_positive",
            "falsepositive",
            "false_positive_tuning_needed",
            "fp_tuning",
            "noise_tuning_needed",
        ),
        ClosureCategory.FALSE_POSITIVE,
    ),
    **dict.fromkeys(
        (
            "undetermined",
            "unable_to_determine",
            "insufficient_info",
            "insufficient_information",
            "unknown",
        ),
        ClosureCategory.UNABLE_TO_RESOLVE,
    ),
    **dict.fromkeys(("duplicate", "dup", "duplicated"), ClosureCategory.DUPLICATE),
}

# Why #3890's normalized shape marked a ticket usable_label: false. Its
# ``undetermined`` is absent: that maps to unable_to_resolve like the raw value.
LABEL_QUALITY_DROPS: Mapping[str, str] = {
    "lazy_bulk": "bulk_close",
    "duplicate": "duplicate",
    "auto_closed": "auto_closed",
    "missing_disposition": "unmapped_disposition",
}

# Raw values in RAW_LABELS' false-positive group that say benign, so the ticket
# record keeps benign and false positive apart (memory's outcome does not).
BENIGN_LABELS = frozenset(
    {
        "bp",
        "benign",
        "benign_positive",
        "benignpositive",
        "benign_expected_activity",
        "expected",
        "expected_activity",
        "authorized_activity",
        "authorized_change",
        "authorized_change_ticket",
        "informationalexpectedactivity",
        "informational_expected_activity",
    }
)

BULK_CLOSE_COUNT = 10
BULK_CLOSE_WINDOW = timedelta(minutes=5)

# Who closed or held a ticket: read for the bulk-close check, then dropped from
# the row before anything else (the stand-in hash, keys, storage) sees it.
CLOSER_KEYS = ("closed_by", "assignee", "assigned_to", "owner")

# The job message names this many unmapped values, then "and N more".
UNMAPPED_SHOWN = 20
UNMAPPED_FIX = "Rename them in the export to a listed value and import again"

# One history import at a time across every API process (#4886). Held on its own
# connection for the whole run; a dead process drops it.
LOCK_KEY = 0x7669_6769_6C48_4953  # "vigilHIS"
RUNNING, DONE, FAILED = "running", "done", "failed"
# Read-time only: a running row whose lock nobody holds.
INCOMPLETE = "incomplete"
INCOMPLETE_ERROR = "Didn't complete: the import stopped before it finished"

_FOLD = re.compile(r"[^a-z0-9]+")
_LIST_SPLIT = re.compile(r"[;,|]")
_ID_KEYS = ("external_id", "ticket_id", "native_id", "id", "number")
_ALERT_ID_KEYS = ("alert_id", "external_id", "id", "finding_id")
_SOURCE_KEYS = ("source_system", "source", "data_source")
_ALERT_TIME_KEYS = (
    "timestamp",
    "observed_at",
    "event_time",
    "ts",
    "time",
    "created_at",
)
_OPENED_KEYS = ("opened_at", "created_at")
_NOTE_KEYS = ("close_notes", "close_note", "resolution", "resolution_notes", "notes")


def parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _raw_key(value: Any) -> str:
    return _FOLD.sub("_", str(value or "").lower()).strip("_")


def _first(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def _row_hash(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {str(k): v for k, v in row.items()}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _ticket_id(row: Mapping[str, Any]) -> Optional[str]:
    """The source's own ticket id; None when the row has none."""
    value = _first(row, _ID_KEYS)
    return None if value is None else str(value)[:500]


def _hypothesis_id(row: Mapping[str, Any], ticket_id: str) -> str:
    # Two ticket systems can use the same number.
    system = str(row.get("source_system") or "")
    if system and not ticket_id.startswith(f"{system}:"):
        return f"{system}:{ticket_id}"
    return ticket_id


def _severity(value: Any) -> str:
    """``normalize_severity``'s level, or ``unknown`` where it would only guess."""
    from core.ingestion.siem_ingestion_service import normalize_severity

    raw = str(value or "").strip().lower()
    level = normalize_severity(raw)
    # Its fallback is medium: only a value that says medium is one.
    if not raw or (
        level == "medium" and not any(w in raw for w in ("med", "3", "warn"))
    ):
        return "unknown"
    return level


def _alert_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    refs = row.get("alert_refs") or row.get("alert_ids") or []
    if isinstance(refs, str):
        refs = _LIST_SPLIT.split(refs)
    refs = [r.get("external_id") if isinstance(r, Mapping) else r for r in refs]
    refs.append(row.get("alert_id"))
    return tuple(str(r).strip() for r in refs if r and str(r).strip())


def _bulk_closed(
    closes: List[Tuple[Optional[str], Optional[datetime]]],
) -> Set[int]:
    """Indexes of tickets one closer closed in a burst: any BULK_CLOSE_COUNT closes
    by the same person within BULK_CLOSE_WINDOW mark every ticket in that burst."""
    by_closer: Dict[str, List[Tuple[datetime, int]]] = defaultdict(list)
    for index, (closer, closed) in enumerate(closes):
        if closed is not None and closer:
            by_closer[closer].append((closed, index))
    bulk: Set[int] = set()
    for stamps in by_closer.values():
        stamps.sort()
        start = 0
        for end in range(len(stamps)):
            while stamps[end][0] - stamps[start][0] > BULK_CLOSE_WINDOW:
                start += 1
            if end - start + 1 >= BULK_CLOSE_COUNT:
                bulk.update(index for _, index in stamps[start : end + 1])
    return bulk


def _drop_reason(row: Mapping[str, Any], repeated: bool) -> Optional[str]:
    if repeated:
        return "duplicate"
    if parse_time(row.get("closed_at")) is None:
        return "no_closed_at"
    if _raw_key(row.get("usable_label")) == "false":
        quality = LABEL_QUALITY_DROPS.get(str(row.get("label_quality")))
        if quality is not None:
            return quality
    category = RAW_LABELS.get(_raw_key(row.get("disposition")))
    if category is ClosureCategory.DUPLICATE or row.get("duplicate_of"):
        return "duplicate"
    if category is None:
        return "unmapped_disposition"
    return None


def _keys(row: Mapping[str, Any]) -> List[str]:
    """Entity Keys a row names in typed fields, never from its prose.

    ``entity_context``/``iocs`` read as a finding's would; ``entities`` may list
    ``{type, value}`` or ``type:value``.
    """
    from core.ingestion.ingestion_service import to_internal_finding

    candidates = list(entity_context_candidates(to_internal_finding(dict(row))))
    listed = row.get("entities") or []
    for item in _LIST_SPLIT.split(listed) if isinstance(listed, str) else listed:
        if isinstance(item, Mapping):
            candidates.append((str(item.get("type", "")), str(item.get("value", ""))))
        elif isinstance(item, str) and ":" in item:
            candidates.append(tuple(item.strip().split(":", 1)))
    keys: List[str] = []
    for kind, value in candidates:
        key = (
            entity_key(kind, value) if kind.strip().lower() in ENTITY_KEY_TYPES else ""
        )
        if key and key not in keys:
            keys.append(key)
    return keys


def import_id(rows: Iterable[Mapping[str, Any]], digest: str, source: str = "") -> str:
    """``<source system>:<short hash of the file>`` from the file's sha256 hex
    digest; the system is the one named, else the rows' commonest, else ``file``."""
    if not source:
        named = Counter(
            str(s).strip() for r in rows if (s := _first(r, _SOURCE_KEYS))
        ).most_common(1)
        source = named[0][0] if named else "file"
    return f"{source.strip()[:64]}:{digest[:12]}"


def _sightings(
    alerts: List[Mapping[str, Any]], fallback_source: str, stats: Counter
) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, List[str]]]]:
    """One Sighting per entity and source system, and each alert's (system, keys)
    by id for the tickets that reference it."""
    groups: Dict[Tuple[str, str], List[datetime]] = defaultdict(list)
    by_id: Dict[str, Tuple[str, List[str]]] = {}
    for alert in alerts:
        system = str(_first(alert, _SOURCE_KEYS) or fallback_source)[:200]
        keys = _keys(alert)
        alert_ref = _first(alert, _ALERT_ID_KEYS)
        if alert_ref:
            by_id[str(alert_ref)] = (system, keys)
        seen = parse_time(_first(alert, _ALERT_TIME_KEYS))
        if seen is None:
            # A Sighting that did not observe its window is not a Sighting.
            stats["alerts_dropped_no_timestamp"] += 1
            continue
        if not keys:
            stats["alerts_dropped_no_entities"] += 1
            continue
        for key in keys:
            groups[(key, system)].append(seen)
    rows = [
        {
            "investigation_kind": KIND.value,
            "entity_key": key,
            "source_system": system,
            "hit_count": len(stamps),
            # Another system's alert text: the conservative side.
            "attacker_influenceable": True,
            "first_seen": min(stamps),
            "last_seen": max(stamps),
            # So imported rows rank below this install's newer ones.
            "concluded_at": max(stamps),
        }
        for (key, system), stamps in groups.items()
    ]
    return rows, by_id


# Stored on history_alerts under that name. Anything else a row carries, apart
# from the consumed id, time and source keys and from columns _keys turns into
# entity keys, is a dropped name: the text is not stored.
_KEPT_ALERT_FIELDS = frozenset({"title", "description", "rule_id", "severity"})
_CONSUMED_ALERT_FIELDS = frozenset((*_SOURCE_KEYS, *_ALERT_ID_KEYS, *_ALERT_TIME_KEYS))
_ENTITY_COLUMN_NAMES = ("entity_context", "iocs", "entities")


def _carried(value: Any) -> bool:
    """A cell the export filled. An empty CSV column is not one."""
    if value is None or value == "":
        return False
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return False
    return True


def _entity_columns(row: Mapping[str, Any], keys: List[str]) -> Set[str]:
    """Columns whose values ``_keys`` kept on this row."""
    kept = set(keys)
    if not kept:
        return set()
    found: Set[str] = set()
    for name in _ENTITY_COLUMN_NAMES:
        if not _carried(row.get(name)):
            continue
        contributed = _keys({name: row[name]})
        if any(key in kept for key in contributed):
            found.add(name)
    return found


def _dropped_names(row: Mapping[str, Any], keys: List[str]) -> List[str]:
    skip = _CONSUMED_ALERT_FIELDS | _KEPT_ALERT_FIELDS | _entity_columns(row, keys)
    return [
        str(key)
        for key, value in row.items()
        if _carried(value) and str(key) not in skip
    ]


def _alerts(
    alerts: List[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], Counter]:
    """One ``history_alerts`` row per alert id, last row in the file winning,
    and the dropped column names with how many alert rows carried each.

    An alert with no id is not stored. A row with no parseable time still is.
    """
    dropped: Counter = Counter()
    by_id: Dict[str, Dict[str, Any]] = {}
    for alert in alerts:
        keys = _keys(alert)
        dropped.update(_dropped_names(alert, keys))
        ref = _first(alert, _ALERT_ID_KEYS)
        if ref is None:
            continue
        rule_id = _first(alert, ("rule_id",))
        by_id[str(ref)[:500]] = {
            "alert_id": str(ref)[:500],
            "alert_time": parse_time(_first(alert, _ALERT_TIME_KEYS)),
            "title": bounded_prose(alert.get("title") or ""),
            "description": bounded_prose(alert.get("description") or ""),
            "rule_id": None if rule_id is None else str(rule_id)[:500],
            "severity": _severity(alert.get("severity")),
            "entity_keys": keys,
        }
    return list(by_id.values()), dropped


def _tickets(
    tickets: List[Mapping[str, Any]],
    alerts_by_id: Mapping[str, Tuple[str, List[str]]],
    fallback_source: str,
    stats: Counter,
    unmapped: Counter,
) -> List[Tuple[Dict[str, Any], Optional[Tuple[Dict[str, Any], List[Dict[str, str]]]]]]:
    """Each kept ticket's ``history_tickets`` row, with its Verdict and sources
    unless it was bulk-closed (those stay out of memory)."""
    closers = [_first(t, CLOSER_KEYS) for t in tickets]
    stats["tickets_bulk_not_tested"] = sum(1 for c in closers if c is None)
    bulk = _bulk_closed(
        [
            (str(c) if c is not None else None, parse_time(t.get("closed_at")))
            for c, t in zip(closers, tickets)
        ]
    )
    # Nothing past this point sees who closed a ticket.
    rows = [{k: v for k, v in t.items() if k not in CLOSER_KEYS} for t in tickets]

    seen_ids: Set[str] = set()
    seen_full: Set[str] = set()
    stand_ins: Counter = Counter()
    out = []
    for index, (ticket, full) in enumerate(zip(rows, tickets)):
        ticket_id = _ticket_id(ticket)
        if ticket_id is not None:
            hypothesis_id = _hypothesis_id(ticket, ticket_id)
            repeated = hypothesis_id in seen_ids
            seen_ids.add(hypothesis_id)
        else:
            # A file with no id column still has to be idempotent across
            # uploads. Rows that differ only in who closed them are two tickets.
            full_hash = _row_hash(full)
            repeated = full_hash in seen_full
            seen_full.add(full_hash)
            stand_in = "row-" + _row_hash(ticket)
            if not repeated:
                stand_ins[stand_in] += 1
            ordinal = stand_ins[stand_in]
            hypothesis_id = stand_in if ordinal == 1 else f"{stand_in}-{ordinal}"
        reason = _drop_reason(ticket, repeated)
        if reason == "unmapped_disposition":
            unmapped[
                str(ticket.get("disposition") or "").strip()[:200] or "(blank)"
            ] += 1
        if reason is not None:
            stats[f"tickets_dropped_{reason}"] += 1
            continue
        raw = _raw_key(ticket.get("disposition"))
        category = RAW_LABELS[raw]
        closed = parse_time(ticket["closed_at"])
        opened = parse_time(_first(ticket, _OPENED_KEYS))
        subjects = _keys(ticket)
        systems: List[str] = []
        for ref in _alert_ids(ticket):
            system, keys = alerts_by_id.get(ref, ("", []))
            subjects += [k for k in keys if k not in subjects]
            if system and system not in systems:
                systems.append(system)
        notes = str(_first(ticket, _NOTE_KEYS) or "").strip()
        severity = _severity(ticket.get("severity"))
        if severity == "unknown":
            stats["tickets_severity_unknown"] += 1
        rule_id = _first(ticket, ("rule_id",))
        is_bulk = index in bulk
        record = {
            "position": index,
            "ticket_id": ticket_id,
            "source_system": str(_first(ticket, _SOURCE_KEYS) or fallback_source)[:200],
            "opened_at": opened,
            "closed_at": closed,
            "disposition_raw": str(ticket.get("disposition")).strip()[:200],
            "category": (
                "benign"
                if category is ClosureCategory.FALSE_POSITIVE and raw in BENIGN_LABELS
                else category.value
            ),
            "severity": severity,
            "rule_id": str(rule_id)[:500] if rule_id is not None else None,
            "entity_keys": subjects,
            # The same cap as history_alerts.alert_id. A connected pull names none.
            "alert_ids": [ref[:500] for ref in _alert_ids(ticket)],
            "title": bounded_prose(ticket.get("title") or ""),
            "notes_fingerprint": (
                hashlib.sha256(notes.encode()).hexdigest() if notes else None
            ),
            "notes_empty": not notes,
            "opened_missing": opened is None,
            "closed_approximate": _raw_key(ticket.get("closed_at_approximate"))
            in ("true", "1", "yes"),
            "bulk_closed": is_bulk,
        }
        if is_bulk:
            # Kept for the record; memory gets nothing, so recall offers nothing.
            stats["tickets_dropped_bulk_close"] += 1
            out.append((record, None))
            continue
        opened = opened or closed
        verdict = {
            "investigation_kind": KIND.value,
            "hypothesis_id": hypothesis_id,
            "statement": record["title"],
            "outcome": CLOSURE_OUTCOMES[category].value,
            "rationale": bounded_prose(notes),
            "subject_entities": subjects[:VERDICT_SUBJECT_CAP],
            "attacker_influenceable_only": False,
            "trust": Trust.ANALYST.value,
            "first_seen": min(opened, closed),
            "last_seen": closed,
            "window_source": WindowSource.ASSERTED.value,
            "concluded_at": closed,
        }
        sources = [
            {
                "source_system": system,
                "stance": Stance.SUPPORTS.value,
                "source_tier": resolve_source_tier(system, TierKind.CASE).value,
            }
            for system in systems
        ]
        out.append((record, (verdict, sources)))
    return out


def _left_out(stats: Mapping[str, int]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for kind in ("tickets", "alerts"):
        prefix = f"{kind}_dropped_"
        reasons = {
            k[len(prefix) :]: n for k, n in stats.items() if k.startswith(prefix) and n
        }
        if reasons:
            out[kind] = reasons
    if stats.get("rows_unreadable"):
        out["rows"] = {"unreadable": stats["rows_unreadable"]}
    return out


def write_import(
    session: Session,
    *,
    investigation_id: str,
    alerts: List[Mapping[str, Any]],
    tickets: List[Mapping[str, Any]],
    actor: str,
    stats: Dict[str, int],
    pull_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Re-derive one import in the caller's transaction: its memory rows, its
    tickets, and its record marked done.

    Counts are set, not added, so uploading the same file twice changes nothing.
    Per-reason drop counts go into ``stats`` (the ingestion job's); the result
    also carries the unmapped disposition values and the dropped alert-column
    names, each with their counts.
    """
    counters: Counter = Counter()
    unmapped: Counter = Counter()
    fallback = investigation_id.rpartition(":")[0] or "file"
    sightings, alerts_by_id = _sightings(alerts, fallback, counters)
    kept = _tickets(tickets, alerts_by_id, fallback, counters, unmapped)
    alert_rows, dropped = _alerts(alerts)
    verdicts = [memory for _, memory in kept if memory is not None]

    clear_investigation(session, KIND, investigation_id)
    session.execute(
        delete(HistoryTicket).where(HistoryTicket.import_id == investigation_id)
    )
    session.execute(
        delete(HistoryAlert).where(HistoryAlert.import_id == investigation_id)
    )
    for row in sightings:
        session.add(EpisodicSighting(investigation_id=investigation_id, **row))
    for row, sources in verdicts:
        verdict = EpisodicVerdict(investigation_id=investigation_id, **row)
        session.add(verdict)
        session.flush()
        for source in sources:
            session.add(EpisodicVerdictSource(verdict_id=verdict.id, **source))

    counts = {"sightings": len(sightings), "verdicts": len(verdicts)}
    stamps = [r["concluded_at"] for r in sightings] + [
        record["closed_at"] for record, _ in kept
    ]
    stats.update(counters)
    stats["sightings_written"] = counts["sightings"]
    stats["verdicts_written"] = counts["verdicts"]
    stats["tickets_kept"] = len(kept)

    now = datetime.now(timezone.utc)
    record = session.get(HistoryImport, investigation_id)
    if record is None:
        record = HistoryImport(id=investigation_id, started_at=now)
        session.add(record)
    record.source_system = fallback
    record.status = DONE
    record.finished_at = now
    record.error = None
    record.rows_read = sum(
        stats.get(k, 0) for k in ("alerts_total", "tickets_total", "rows_unreadable")
    )
    record.tickets_kept = len(kept)
    record.verdicts_written = counts["verdicts"]
    record.sightings_written = counts["sightings"]
    record.left_out = _left_out(stats)
    record.bulk_not_tested = counters["tickets_bulk_not_tested"]
    record.severity_unknown = counters["tickets_severity_unknown"]
    record.unmapped = dict(unmapped.most_common())
    record.dropped_columns = dict(dropped.most_common())
    record.pull_days = pull_days
    record.pulled_at = now if pull_days is not None else None
    record.concluded_at = max(stamps, default=None)
    record.has_ticket_detail = True
    session.flush()
    session.add_all(HistoryTicket(import_id=investigation_id, **row) for row, _ in kept)
    session.add_all(
        HistoryAlert(import_id=investigation_id, **row) for row in alert_rows
    )

    trail_events.record(
        session,
        IMPORT_WRITTEN,
        actor=actor,
        import_id=investigation_id,
        verdicts=counts["verdicts"],
        sightings=counts["sightings"],
    )
    return {
        **counts,
        "tickets": len(kept),
        "unmapped": dict(unmapped),
        "dropped_columns": dict(dropped),
    }


def describe_unmapped(unmapped: Mapping[str, int]) -> Optional[str]:
    """The job message's line naming unmapped values, most common first."""
    if not unmapped:
        return None
    ranked = Counter(unmapped).most_common()
    named = ", ".join(f"'{value}' ({n})" for value, n in ranked[:UNMAPPED_SHOWN])
    more = len(ranked) - UNMAPPED_SHOWN
    if more > 0:
        named += f" and {more} more"
    return f"Unmapped disposition values: {named}. {UNMAPPED_FIX}"


def describe_dropped(dropped: Mapping[str, int]) -> Optional[str]:
    """The job message's line naming dropped alert columns, most common first."""
    if not dropped:
        return None
    ranked = Counter(dropped).most_common()
    named = ", ".join(f"'{name}' ({n})" for name, n in ranked[:UNMAPPED_SHOWN])
    more = len(ranked) - UNMAPPED_SHOWN
    if more > 0:
        named += f" and {more} more"
    return f"Dropped columns: {named}"


# --- The run: one at a time, and a start that outlives a crash ---------------


def take_import_lock() -> Optional[Connection]:
    """The import lock on its own connection, or None when another run holds it.

    The caller keeps the connection for the whole run and hands it to
    ``release_import_lock``.
    """
    manager = get_db_manager()
    if manager.engine is None:
        manager.initialize()
    conn = manager.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}
        ).scalar():
            return conn
    except Exception:
        conn.close()
        raise
    conn.close()
    return None


def release_import_lock(conn: Connection) -> None:
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    except Exception:  # noqa: BLE001 - a dead session has lost the lock already
        # Never pool a connection that might still hold it.
        conn.invalidate()
    finally:
        conn.close()


def _lock_held(session: Session) -> bool:
    """Whether any session holds the import lock, without taking it."""
    return bool(
        session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
                " AND database = (SELECT oid FROM pg_database"
                " WHERE datname = current_database())"
                " AND classid = :hi AND objid = :lo AND objsubid = 1 AND granted)"
            ),
            {"hi": LOCK_KEY >> 32, "lo": LOCK_KEY & 0xFFFFFFFF},
        )
    )


def start_run(investigation_id: str, pull_days: Optional[int] = None) -> None:
    """Commit the import's ``running`` row before its work starts, so a crash
    leaves it visible. The caller holds the import lock, so any other running
    row is a run that died: it is marked failed. A connected pull records its
    window now, so one that fails or dies can still be re-run as it was."""
    now = datetime.now(timezone.utc)
    with unit_of_work() as session:
        session.execute(
            update(HistoryImport)
            .where(HistoryImport.status == RUNNING)
            .values(status=FAILED, error=INCOMPLETE_ERROR, finished_at=now)
        )
        record = session.get(HistoryImport, investigation_id)
        if record is None:
            record = HistoryImport(
                id=investigation_id,
                source_system=investigation_id.rpartition(":")[0] or "file",
            )
            session.add(record)
        record.status = RUNNING
        if pull_days is not None:
            record.pull_days = pull_days
        record.started_at = now
        record.finished_at = None
        record.error = None


def fail_run(investigation_id: str, error: str) -> None:
    """Mark the import's running row failed, with what stopped it."""
    with unit_of_work() as session:
        session.execute(
            update(HistoryImport)
            .where(
                HistoryImport.id == investigation_id, HistoryImport.status == RUNNING
            )
            .values(
                status=FAILED,
                error=error[:2000],
                finished_at=datetime.now(timezone.utc),
            )
        )


class ImportRunning(RuntimeError):
    """The import is being written right now."""


def _import_row(record: HistoryImport, locked: bool) -> Dict[str, Any]:
    status = record.status
    error = record.error
    if status == RUNNING and not locked:
        status, error = INCOMPLETE, INCOMPLETE_ERROR
    return {
        "import_id": record.id,
        "source_system": record.source_system,
        "status": status,
        "error": error,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "rows_read": record.rows_read,
        "tickets_kept": record.tickets_kept,
        "verdicts_written": record.verdicts_written,
        "sightings_written": record.sightings_written,
        "left_out": record.left_out or {},
        "bulk_not_tested": record.bulk_not_tested,
        "severity_unknown": record.severity_unknown,
        "unmapped": [
            {"value": value, "count": n}
            for value, n in Counter(record.unmapped or {}).most_common()
        ],
        "dropped_columns": [
            {"name": name, "count": n}
            for name, n in Counter(record.dropped_columns or {}).most_common()
        ],
        "pull_days": record.pull_days,
        "pulled_at": record.pulled_at,
        "has_ticket_detail": record.has_ticket_detail,
        "concluded_at": record.concluded_at,
        # When it was (last) run.
        "imported_at": record.finished_at or record.started_at,
    }


def list_imports(session: Session) -> List[Dict[str, Any]]:
    """Every import, newest run first."""
    records = list(
        session.scalars(
            select(HistoryImport).order_by(
                HistoryImport.started_at.desc(), HistoryImport.id
            )
        )
    )
    locked = any(r.status == RUNNING for r in records) and _lock_held(session)
    return [_import_row(r, locked) for r in records]


def delete_import(
    session: Session, investigation_id: str, *, actor: str
) -> Optional[Dict[str, Any]]:
    """Clear one whole import: its memory rows, its tickets, its alerts and its record.
    None when there is no such import; ImportRunning while any import runs, since
    a re-run of this one could be about to write it again."""
    # Locked first, so a re-run that is writing it commits before this clears.
    record = session.get(HistoryImport, investigation_id, with_for_update=True)
    if record is None:
        return None
    if _lock_held(session):
        raise ImportRunning(investigation_id)
    row = _import_row(record, False)
    # The ledgers stay. The count is for the result; the erase is a later call.
    kept = marked_run_ids(session, investigation_id)
    clear_investigation(session, KIND, investigation_id)
    session.execute(
        delete(HistoryTicket).where(HistoryTicket.import_id == investigation_id)
    )
    session.execute(
        delete(HistoryAlert).where(HistoryAlert.import_id == investigation_id)
    )
    session.execute(delete(HistoryImport).where(HistoryImport.id == investigation_id))
    trail_events.record(
        session,
        IMPORT_DELETED,
        actor=actor,
        import_id=investigation_id,
        verdicts=row["verdicts_written"],
        sightings=row["sightings_written"],
    )
    row["ledgers_kept"] = len(kept)
    row["run_ids"] = kept
    row["erase_permission"] = LEDGERS_ERASE
    return row
