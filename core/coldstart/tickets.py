"""Closed tickets as labels, with the ones not worth trusting dropped and counted.

A customer's closure habits are noisy: tickets bulk-closed at end of shift, closed as
duplicates, or closed with no decision. A kept ticket is one a person decided.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from core.coldstart.baseline import (
    Alert,
    Outcome,
    parse_time,
    signature_for,
    signature_of,
)
from core.ingestion.ingestion_service import _utf8_lines


class Label(str, Enum):
    BENIGN_POSITIVE = "benign_positive"
    FALSE_POSITIVE = "false_positive"
    TRUE_POSITIVE = "true_positive"
    UNKNOWN = "unknown"


class Drop(str, Enum):
    UNMAPPED = "unmapped_disposition"
    NO_CLOSED_AT = "no_closed_at"
    BULK_CLOSE = "bulk_close"
    DUPLICATE = "duplicate"
    UNDETERMINED = "undetermined"
    UNREADABLE = "unreadable"


# Raw values, lower-cased with runs of other characters folded to "_". A value not
# listed is reported as unmapped, never guessed.
RAW_LABELS: Mapping[str, Label] = {
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
        Label.TRUE_POSITIVE,
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
        ),
        Label.BENIGN_POSITIVE,
    ),
    **dict.fromkeys(
        (
            "fp",
            "false_positive",
            "falsepositive",
            "false_positive_tuning_needed",
            "fp_tuning",
            "noise_tuning_needed",
        ),
        Label.FALSE_POSITIVE,
    ),
    **dict.fromkeys(
        (
            "undetermined",
            "unable_to_determine",
            "insufficient_info",
            "insufficient_information",
            "unknown",
        ),
        Label.UNKNOWN,
    ),
}
RAW_DUPLICATE = frozenset({"duplicate", "dup", "duplicated"})

# Why the normalized shape marked a ticket usable_label: false.
LABEL_QUALITY_DROPS: Mapping[str, Drop] = {
    "lazy_bulk": Drop.BULK_CLOSE,
    "duplicate": Drop.DUPLICATE,
    "auto_closed": Drop.UNDETERMINED,
    "missing_disposition": Drop.UNDETERMINED,
    "undetermined": Drop.UNDETERMINED,
}

BULK_CLOSE_COUNT = 10
BULK_CLOSE_WINDOW = timedelta(minutes=5)

_FOLD = re.compile(r"[^a-z0-9]+")
_ENTITY_SPLIT = re.compile(r"[;,|]")


@dataclass(frozen=True)
class Ticket:
    external_id: str
    disposition: Label
    closed_at: datetime
    alert_ids: Tuple[str, ...] = ()
    signature: Optional[str] = None
    entities: FrozenSet[str] = frozenset()
    source_system: Optional[str] = None


@dataclass
class FilteredTickets:
    kept: List[Ticket] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)


def _raw_key(value: Any) -> str:
    return _FOLD.sub("_", str(value or "").lower()).strip("_")


def _external_id(row: Mapping[str, Any]) -> str:
    system = str(row.get("source_system") or "")
    for key in ("external_id", "ticket_id", "native_id", "id", "number"):
        if row.get(key):
            value = str(row[key])
            # Two ticket systems can use the same number. Treating them as duplicates
            # could drop the one real ticket that keeps a kind from closing.
            return (
                f"{system}:{value}"
                if system and not value.startswith(f"{system}:")
                else value
            )
    # A file with no id column still has to be idempotent across uploads.
    canonical = json.dumps(row, sort_keys=True, default=str)
    return "row-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _alert_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    ids: List[str] = []
    for ref in row.get("alert_refs") or []:
        value = ref.get("external_id") if isinstance(ref, Mapping) else ref
        if value:
            ids.append(str(value))
    if row.get("alert_id"):
        ids.append(str(row["alert_id"]))
    return tuple(ids)


def _entities(row: Mapping[str, Any]) -> FrozenSet[str]:
    value = row.get("entities") or ()
    if isinstance(value, str):
        value = _ENTITY_SPLIT.split(value)
    return frozenset(str(e).strip() for e in value if str(e).strip())


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


def _drop_reason(row: Mapping[str, Any], repeated: bool, bulk: bool) -> Optional[Drop]:
    if repeated:
        return Drop.DUPLICATE
    if parse_time(row.get("closed_at")) is None:
        return Drop.NO_CLOSED_AT
    if row.get("usable_label") is False:
        return LABEL_QUALITY_DROPS.get(str(row.get("label_quality")), Drop.UNDETERMINED)
    raw = _raw_key(row.get("disposition"))
    if raw in RAW_DUPLICATE or row.get("duplicate_of"):
        return Drop.DUPLICATE
    label = RAW_LABELS.get(raw)
    if label is None:
        return Drop.UNMAPPED
    if label is Label.UNKNOWN:
        return Drop.UNDETERMINED
    return Drop.BULK_CLOSE if bulk else None


def filter_tickets(rows: Iterable[Mapping[str, Any]]) -> FilteredTickets:
    rows = list(rows)
    bulk = _bulk_closed(rows)
    result = FilteredTickets()
    seen: Set[str] = set()
    for index, row in enumerate(rows):
        external_id = _external_id(row)
        reason = _drop_reason(row, external_id in seen, index in bulk)
        seen.add(external_id)
        if reason is not None:
            result.dropped[reason.value] += 1
            continue
        entities = _entities(row)
        title = row.get("title")
        result.kept.append(
            Ticket(
                external_id=external_id,
                disposition=RAW_LABELS[_raw_key(row.get("disposition"))],
                closed_at=parse_time(row["closed_at"]),
                alert_ids=_alert_ids(row),
                signature=(
                    signature_of(
                        str(row.get("source") or row.get("source_system") or "unknown"),
                        str(title),
                        entities,
                    )
                    if title
                    else None
                ),
                entities=entities,
                source_system=row.get("source_system") or None,
            )
        )
    return result


def outcomes_from(
    tickets: Iterable[Ticket], alerts: Mapping[str, Alert]
) -> Tuple[List[Outcome], int]:
    """One Outcome per distinct alert a ticket closed, as the held-out eval counts them,
    else one by source and title. A ticket cannot have closed an alert observed after
    it closed, so that link counts for nothing. Returns them with how many tickets link
    to nothing."""
    outcomes: List[Outcome] = []
    unlinked = 0
    for ticket in tickets:
        referenced = [alerts[i] for i in dict.fromkeys(ticket.alert_ids) if i in alerts]
        linked = [a for a in referenced if a.observed_at <= ticket.closed_at]
        if linked:
            outcomes.extend(
                Outcome(
                    signature_for(alert),
                    ticket.disposition.value,
                    ticket.closed_at,
                    alert.entities,
                    ticket.external_id,
                )
                for alert in linked
            )
        elif ticket.signature and not referenced:
            outcomes.append(
                Outcome(
                    ticket.signature,
                    ticket.disposition.value,
                    ticket.closed_at,
                    ticket.entities,
                    ticket.external_id,
                )
            )
        else:
            unlinked += 1
    return outcomes, unlinked


def _json_rows(path: Path) -> Iterator[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("tickets", []) if isinstance(data, Mapping) else data
    if not isinstance(rows, list):
        raise ValueError("Expected a JSON array of tickets or {tickets: [...]}.")
    yield from rows


def _jsonl_rows(path: Path) -> Iterator[Any]:
    for line in _utf8_lines(path):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            yield None


def _csv_rows(path: Path) -> Iterator[Any]:
    yield from csv.DictReader(_utf8_lines(path))


_READERS = {"json": _json_rows, "jsonl": _jsonl_rows, "csv": _csv_rows}
FORMATS = frozenset(_READERS)


def parse_ticket_file(path: Path, fmt: str) -> FilteredTickets:
    """Read and filter a ticket export. A file that cannot be read at all raises
    ValueError; one bad row in it is counted as unreadable."""
    try:
        rows = list(_READERS[fmt](path))
    except (ValueError, csv.Error) as e:
        raise ValueError(f"Could not read the {fmt} file: {e}") from e
    usable = [row for row in rows if isinstance(row, Mapping)]
    result = filter_tickets(usable)
    if len(rows) != len(usable):
        result.dropped[Drop.UNREADABLE.value] += len(rows) - len(usable)
    return result
