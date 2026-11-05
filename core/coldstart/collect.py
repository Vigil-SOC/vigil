"""Read alert history into a run's snapshots, from a source or an uploaded file.

Rows go to ``cold_start_alerts`` only, never to ``findings``, so nothing read here can
trigger triage or response.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
)

from core.coldstart.baseline import ENTITY_KEYS, alert_from_finding, signature_for
from core.federation.contract import FederationAdapter
from core.ingestion.ingestion_service import (
    IngestionService,
    _content_finding_id,
    _utf8_lines,
    row_identity_key,
    to_internal_finding,
)

BATCH_SIZE = 500
MAX_STALE_READS = 3
MAX_READS = 2_000
# Oldest first, so hitting the cap loses the newest alerts. It is reported, not silent.
MAX_ROWS = 200_000

DROP_NO_TIME = "no_time"
DROP_UNREADABLE = "unreadable"
DROP_LOGLM_NOT_INCIDENT = "loglm_not_incident"

LOGLM_SOURCE = "loglm"
UPLOAD_SOURCE = "upload"


@dataclass
class CollectResult:
    read: int = 0
    dropped: Counter = field(default_factory=Counter)
    capped: bool = False


@dataclass
class ParsedAlerts:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)


def _no_nul(value: str) -> str:
    # Postgres text and jsonb refuse NUL, and one alert must not fail a whole run.
    return value.replace("\x00", "")


def alert_row(finding: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A finding from an adapter or a file as a ``cold_start_alerts`` row. None when
    it has no usable time. Its id is the source's own, the one tickets refer to."""
    internal = to_internal_finding(dict(finding))
    alert = alert_from_finding(internal)
    if alert is None:
        return None
    entities = sorted(alert.entities)
    alert_id = str(
        internal.get("external_id") or finding.get("alert_id") or alert.alert_id
    ) or (
        "row-"
        + hashlib.sha256(
            json.dumps(
                [alert.source, alert.title, alert.observed_at.isoformat(), entities]
            ).encode()
        ).hexdigest()[:32]
    )
    return {
        "alert_id": _no_nul(alert_id),
        "source": _no_nul(alert.source),
        "signature": _no_nul(signature_for(alert)),
        "observed_at": alert.observed_at,
        "title": _no_nul(alert.title),
        "severity": _no_nul(alert.severity),
        "entities": [_no_nul(e) for e in entities],
        "payload": {"description": _no_nul(str(internal.get("description") or ""))},
    }


async def collect_history(
    adapter: FederationAdapter,
    *,
    start: datetime,
    store_batch: Callable[[List[Dict[str, Any]]], Awaitable[None]],
    max_rows: int = MAX_ROWS,
) -> CollectResult:
    """Page an adapter from ``start`` to now by seeding its cursor.

    Every real adapter reads ``last_poll_at`` through ``parse_cursor_since`` and pages
    oldest first. Three things the contract does not say:

    - A full batch's cursor stops at its newest alert and the next read starts there
      inclusively, so the boundary comes back and ids are what dedupe it. A batch
      stuck on one instant steps past it and can add nothing while there is still
      more behind it. An unreported drop makes a repeating batch look short, so nothing
      new alone does not end the walk. An empty short read, an unchanged cursor, or
      ``MAX_STALE_READS`` short reads in a row do. A full page that moved the cursor is
      progress even if every record in it was dropped, so only ``MAX_READS`` bounds those,
      and hitting it reports the walk as capped.
    - A drained batch's cursor moves to now, and the next read is empty. That is how
      the walk ends: Splunk and CrowdStrike do not report drops, so a short list of
      findings cannot be told from a short read.
    - CrowdStrike and Sentinel read the whole remaining window on each fetch and then
      cut it to ``max_items``, so a long history costs them more than it looks.
    """
    # parse_cursor_since keeps an offset, and adapters compare the result with their
    # naive UTC clock, so an aware seed would raise TypeError on the first fetch.
    cursor: Dict[str, Any] = {
        "last_poll_at": start.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    }
    result = CollectResult()
    seen: Set[str] = set()
    stale_reads = 0
    reads = 0
    while len(seen) < max_rows and reads < MAX_READS:
        reads += 1
        batch = await adapter.fetch(cursor=cursor, max_items=BATCH_SIZE)
        if batch.dropped:
            result.dropped[DROP_UNREADABLE] += batch.dropped
        fresh: List[Dict[str, Any]] = []
        for finding in batch.findings:
            row = alert_row(finding)
            if row is None:
                result.dropped[DROP_NO_TIME] += 1
            elif row["alert_id"] not in seen:
                seen.add(row["alert_id"])
                fresh.append(row)
        if fresh:
            await store_batch(fresh[: max_rows - result.read])
            result.read = min(len(seen), max_rows)
            stale_reads = 0
        else:
            full = len(batch.findings) + batch.dropped >= BATCH_SIZE
            stale_reads = 0 if full and batch.cursor != cursor else stale_reads + 1
            if (
                (not batch.findings and not full)
                or batch.cursor == cursor
                or stale_reads >= MAX_STALE_READS
            ):
                break
        cursor = batch.cursor
    result.capped = len(seen) >= max_rows or reads >= MAX_READS
    return result


def _top_tactic(predictions: Any) -> Optional[str]:
    if not isinstance(predictions, Mapping) or not predictions:
        return None
    return max(predictions, key=lambda k: predictions[k] or 0)


def _loglm_finding(service: IngestionService, row: Dict[str, Any]) -> Dict[str, Any]:
    """A LogLM row has no title, and its signature would group every flow as one kind
    without one. The tactic it predicts is the nearest thing to a detection name."""
    finding = service._parquet_row_to_finding(row, LOGLM_SOURCE)
    tactic = _top_tactic(finding.get("mitre_predictions"))
    finding["title"] = f"LogLM flow: {tactic}" if tactic else "LogLM flow"
    finding["description"] = "LogLM flagged this flow sequence as an incident."
    return finding


def _parquet_findings(path: Path, dropped: Counter) -> Iterator[Mapping[str, Any]]:
    import pyarrow.parquet as pq

    service = IngestionService()
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    is_loglm = service._detect_parquet_schema(set(names)) == "loglm"
    columns = [name for name in names if name != "embedding"]
    for batch in parquet.iter_batches(batch_size=1000, columns=columns):
        for row in batch.to_pylist():
            if is_loglm and row.get("incident_pred") == 0:
                dropped[DROP_LOGLM_NOT_INCIDENT] += 1
                continue
            try:
                if is_loglm:
                    yield _loglm_finding(service, row)
                else:
                    yield _generic_parquet_finding(service, row)
            except Exception:  # one malformed row must not lose the file
                dropped[DROP_UNREADABLE] += 1


def _generic_parquet_finding(
    service: IngestionService, row: Dict[str, Any]
) -> Dict[str, Any]:
    """The shared converter names a row by its content under one source and reads only
    its addresses. Tickets refer to the row's own id, and new alerts match on its source
    and entities, so those stay."""
    source = row.get("data_source") or row.get("source") or UPLOAD_SOURCE
    finding = {
        **service._generic_row_to_finding(row, str(source)),
        **_named(row),
    }
    identity = next(
        (row[k] for k in ("finding_id", "id", "alert_id") if row.get(k)), None
    )
    if identity:
        finding["finding_id"] = str(identity)
    if row.get("external_id"):
        finding["external_id"] = str(row["external_id"])
    nested = [row.get(key) for key in ("iocs", "entity_context")]
    values = {
        **{k: v for n in nested if isinstance(n, Mapping) for k, v in n.items()},
        **row,
    }
    for key in ENTITY_KEYS:
        if values.get(key):
            finding["entity_context"][key] = values[key]
    return finding


def _named(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The columns the shared CSV and Parquet rows leave out, which a signature needs."""
    return {key: row[key] for key in ("title", "description") if row.get(key)}


def _csv_findings(path: Path, dropped: Counter) -> Iterator[Mapping[str, Any]]:
    service = IngestionService()
    for row in csv.DictReader(_utf8_lines(path)):
        identity = next(
            (row[k] for k in ("finding_id", "id", "alert_id") if row.get(k)), None
        )
        try:
            # The shared converter mints a random id when there is none, which would
            # make a second upload of the same file look like new alerts.
            finding = service._csv_row_to_finding(
                {
                    **row,
                    "finding_id": identity
                    or _content_finding_id(
                        row_identity_key(row, tuple(sorted(row.keys())))
                    ),
                }
            )
        except Exception:  # one malformed row must not lose the file
            dropped[DROP_UNREADABLE] += 1
            continue
        if not row.get("data_source") and row.get("source"):
            finding["data_source"] = row["source"]
        if row.get("external_id"):
            finding["external_id"] = row["external_id"]
        yield {**finding, **_named(row)}


def _json_findings(path: Path, dropped: Counter) -> Iterator[Mapping[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        data = data.get("findings") or data.get("alerts") or []
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of alerts or {findings: [...]}.")
    yield from data


def _jsonl_findings(path: Path, dropped: Counter) -> Iterator[Mapping[str, Any]]:
    for line in _utf8_lines(path):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            dropped[DROP_UNREADABLE] += 1


_READERS = {
    "json": _json_findings,
    "jsonl": _jsonl_findings,
    "csv": _csv_findings,
    "parquet": _parquet_findings,
}
FORMATS = frozenset(_READERS)


def parse_alert_file(path: Path, fmt: str, max_rows: int = MAX_ROWS) -> ParsedAlerts:
    """Read an uploaded alert export. A file that cannot be read at all raises
    ValueError; one bad row in it is counted as unreadable."""
    parsed = ParsedAlerts()
    seen: Dict[str, str] = {}
    try:
        for finding in _READERS[fmt](path, parsed.dropped):
            if not isinstance(finding, Mapping):
                parsed.dropped[DROP_UNREADABLE] += 1
                continue
            row = alert_row(finding)
            if row is None:
                parsed.dropped[DROP_NO_TIME] += 1
            elif row["alert_id"] not in seen:
                seen[row["alert_id"]] = row["source"]
                parsed.rows.append(row)
                if len(parsed.rows) >= max_rows:
                    break
            elif seen[row["alert_id"]] != row["source"]:
                # Tickets name alerts by id alone, so keeping one would hand it the
                # other's tickets, and that can make a real alert look clean.
                raise ValueError(
                    f"Alert id {row['alert_id']} is used by both "
                    f"{seen[row['alert_id']]} and {row['source']}."
                )
    except (ValueError, csv.Error, OSError) as e:  # pyarrow's errors subclass these
        raise ValueError(f"Could not read the {fmt} file: {e}") from e
    return parsed
