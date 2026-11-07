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
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.cases.closure import CLOSURE_OUTCOMES, ClosureCategory
from core.memory.distil import clear_investigation, write_marker
from core.memory.entity_keys import entity_context_candidates, entity_key
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
from core.storage.models import (
    EpisodicDistilMarker,
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
)

KIND = InvestigationKind.IMPORTED
IMPORT_MAPPING_VERSION = 1
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

BULK_CLOSE_COUNT = 10
BULK_CLOSE_WINDOW = timedelta(minutes=5)

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


def _external_id(row: Mapping[str, Any]) -> str:
    system = str(row.get("source_system") or "")
    for key in _ID_KEYS:
        if row.get(key):
            value = str(row[key])
            # Two ticket systems can use the same number.
            return (
                f"{system}:{value}"
                if system and not value.startswith(f"{system}:")
                else value
            )
    # A file with no id column still has to be idempotent across uploads.
    canonical = json.dumps(
        {str(k): v for k, v in row.items()}, sort_keys=True, default=str
    )
    return "row-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _alert_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    refs = row.get("alert_refs") or row.get("alert_ids") or []
    if isinstance(refs, str):
        refs = _LIST_SPLIT.split(refs)
    refs = [r.get("external_id") if isinstance(r, Mapping) else r for r in refs]
    refs.append(row.get("alert_id"))
    return tuple(str(r).strip() for r in refs if r and str(r).strip())


def _bulk_closed(rows: List[Mapping[str, Any]]) -> Set[int]:
    """Indexes of tickets one closer closed in a burst: any BULK_CLOSE_COUNT closes
    by the same person within BULK_CLOSE_WINDOW mark every ticket in that burst."""
    by_closer: Dict[str, List[Tuple[datetime, int]]] = defaultdict(list)
    for index, row in enumerate(rows):
        closed = parse_time(row.get("closed_at"))
        if closed is not None and row.get("closed_by"):
            by_closer[str(row["closed_by"])].append((closed, index))
    bulk: Set[int] = set()
    for closes in by_closer.values():
        closes.sort()
        start = 0
        for end in range(len(closes)):
            while closes[end][0] - closes[start][0] > BULK_CLOSE_WINDOW:
                start += 1
            if end - start + 1 >= BULK_CLOSE_COUNT:
                bulk.update(index for _, index in closes[start : end + 1])
    return bulk


def _drop_reason(row: Mapping[str, Any], repeated: bool, bulk: bool) -> Optional[str]:
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
    return "bulk_close" if bulk else None


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


def import_id(
    rows: Iterable[Mapping[str, Any]], content: bytes, source: str = ""
) -> str:
    """``<source system>:<short hash of the file>``; the system is the one named,
    else the rows' commonest, else ``file``."""
    if not source:
        named = Counter(
            str(s).strip() for r in rows if (s := _first(r, _SOURCE_KEYS))
        ).most_common(1)
        source = named[0][0] if named else "file"
    return f"{source.strip()[:64]}:{hashlib.sha256(content).hexdigest()[:12]}"


def _sightings(
    alerts: List[Mapping[str, Any]], fallback_source: str, stats: Dict[str, int]
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


def _verdicts(
    tickets: List[Mapping[str, Any]],
    alerts_by_id: Mapping[str, Tuple[str, List[str]]],
    stats: Dict[str, int],
) -> List[Tuple[Dict[str, Any], List[Dict[str, str]]]]:
    bulk = _bulk_closed(tickets)
    seen: Set[str] = set()
    out = []
    for index, ticket in enumerate(tickets):
        external_id = _external_id(ticket)
        reason = _drop_reason(ticket, external_id in seen, index in bulk)
        seen.add(external_id)
        if reason is not None:
            stats[f"tickets_dropped_{reason}"] += 1
            continue
        category = RAW_LABELS[_raw_key(ticket.get("disposition"))]
        closed = parse_time(ticket["closed_at"])
        opened = parse_time(_first(ticket, _OPENED_KEYS)) or closed
        subjects = _keys(ticket)
        systems: List[str] = []
        for ref in _alert_ids(ticket):
            system, keys = alerts_by_id.get(ref, ("", []))
            subjects += [k for k in keys if k not in subjects]
            if system and system not in systems:
                systems.append(system)
        verdict = {
            "investigation_kind": KIND.value,
            "hypothesis_id": external_id,
            "statement": bounded_prose(ticket.get("title") or ""),
            "outcome": CLOSURE_OUTCOMES[category].value,
            "rationale": bounded_prose(_first(ticket, _NOTE_KEYS) or ""),
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
        out.append((verdict, sources))
    return out


def write_import(
    session: Session,
    *,
    investigation_id: str,
    alerts: List[Mapping[str, Any]],
    tickets: List[Mapping[str, Any]],
    actor: str,
    stats: Dict[str, int],
) -> Dict[str, int]:
    """Re-derive one import in the caller's transaction: clear, insert, marker.

    Counts are set, not added, so uploading the same file twice changes nothing.
    Per-reason drop counts go into ``stats`` (the ingestion job's).
    """
    counters: Counter = Counter()
    fallback = investigation_id.rpartition(":")[0] or "file"
    sightings, alerts_by_id = _sightings(alerts, fallback, counters)
    verdicts = _verdicts(tickets, alerts_by_id, counters)

    clear_investigation(session, KIND, investigation_id)
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
        r["concluded_at"] for r, _ in verdicts
    ]
    write_marker(
        session,
        kind=KIND,
        investigation_id=investigation_id,
        version=IMPORT_MAPPING_VERSION,
        counts=counts,
        concluded_at=max(stamps, default=datetime.now(timezone.utc)),
    )
    trail_events.record(
        session,
        IMPORT_WRITTEN,
        actor=actor,
        import_id=investigation_id,
        verdicts=counts["verdicts"],
        sightings=counts["sightings"],
    )
    stats.update(counters)
    stats["sightings_written"] = counts["sightings"]
    stats["verdicts_written"] = counts["verdicts"]
    return counts


def _import_row(marker: EpisodicDistilMarker) -> Dict[str, Any]:
    return {
        "import_id": marker.investigation_id,
        "source_system": marker.investigation_id.rpartition(":")[0],
        "verdicts_written": marker.verdicts_written,
        "sightings_written": marker.sightings_written,
        "concluded_at": marker.concluded_at,
        "imported_at": marker.distilled_at,
    }


def list_imports(session: Session) -> List[Dict[str, Any]]:
    """Every import, newest upload first."""
    markers = session.scalars(
        select(EpisodicDistilMarker)
        .where(EpisodicDistilMarker.investigation_kind == KIND.value)
        .order_by(
            EpisodicDistilMarker.distilled_at.desc(),
            EpisodicDistilMarker.investigation_id,
        )
    )
    return [_import_row(m) for m in markers]


def delete_import(
    session: Session, investigation_id: str, *, actor: str
) -> Optional[Dict[str, Any]]:
    """Clear one whole import and its marker; None when there is no such import."""
    marker = session.get(EpisodicDistilMarker, (KIND.value, investigation_id))
    if marker is None:
        return None
    row = _import_row(marker)
    clear_investigation(session, KIND, investigation_id)
    session.execute(
        delete(EpisodicDistilMarker).where(
            EpisodicDistilMarker.investigation_kind == KIND.value,
            EpisodicDistilMarker.investigation_id == investigation_id,
        )
    )
    trail_events.record(
        session,
        IMPORT_DELETED,
        actor=actor,
        import_id=investigation_id,
        verdicts=row["verdicts_written"],
        sightings=row["sightings_written"],
    )
    return row
