"""Push-source writes to the Integration Health record, throttled per process.

A change of state is written at once: the first success, a failure, or a
recovery, and the first unreadable record of the hour. Otherwise received and
dropped counts, and the labels seen with them, stay in memory and flush on the
next event after a minute. No queue and no timer. Counts still in memory when
the process exits are lost. A failed write is logged and does not fail ingestion.

The source ids are the ones ``core.integrations.health`` already judges.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

from core.storage.source_health import (
    DROP_WINDOW,
    LABEL_CAP,
    record_collected,
    record_dropped,
    record_failed,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

KAFKA_SOURCE = "kafka"
DAEMON_SOURCE = "webhook:daemon"
DARKTRACE_SOURCE = "webhook:darktrace"
CLOUDFLARE_SOURCE = "webhook:cloudflare"

# core.federation.error_kind.KINDS. This module cannot import federation.
_KINDS = ("auth", "network", "config", "quota", "upstream", "unknown")
_FLUSH_SECONDS = 60.0
_LABEL_MAX = 128

_lock = threading.Lock()


@dataclass
class _Bucket:
    received: int = 0
    dropped: int = 0
    labels: Dict[str, str] = field(default_factory=dict)
    want_success: bool = False
    flushed_mono: float = 0.0
    stored_success: bool = False
    stored_failing: bool = False
    drop_flushed_at: Optional[datetime] = None


@dataclass
class _Snap:
    source_id: str
    received: int
    dropped: int
    labels: Dict[str, str]
    want_success: bool
    failure: Optional[Tuple[str, str]]
    changed: bool


_buckets: Dict[str, _Bucket] = {}


def _remember(labels: Optional[Iterable[str]], seen: str, dest: Dict[str, str]) -> None:
    if not labels:
        return
    if isinstance(labels, str):
        labels = (labels,)
    for raw in labels:
        if not isinstance(raw, str):
            continue
        text = raw.strip()[:_LABEL_MAX]
        if text:
            dest[text] = seen
    if len(dest) > LABEL_CAP:
        kept = sorted(dest.items(), key=lambda kv: kv[1], reverse=True)[:LABEL_CAP]
        dest.clear()
        dest.update(kept)


def _drop_open(bucket: _Bucket, now: datetime) -> bool:
    at = bucket.drop_flushed_at
    return at is not None and now - at < DROP_WINDOW


def _take(
    source_id: str,
    *,
    success: bool,
    received: int,
    dropped: int,
    labels: Optional[Iterable[str]],
    failure: Optional[Tuple[str, str]],
) -> Optional[_Snap]:
    now = utcnow()
    mono = time.monotonic()
    with _lock:
        bucket = _buckets.setdefault(source_id, _Bucket())
        bucket.received += max(received, 0)
        bucket.dropped += max(dropped, 0)
        if success:
            bucket.want_success = True
        _remember(labels, now.isoformat(), bucket.labels)
        changed = bool(failure)
        if success and (not bucket.stored_success or bucket.stored_failing):
            changed = True
        if dropped > 0 and not _drop_open(bucket, now):
            changed = True
        due = bucket.flushed_mono == 0.0 or mono - bucket.flushed_mono >= _FLUSH_SECONDS
        if not changed and not due:
            return None
        snap = _Snap(
            source_id,
            bucket.received,
            bucket.dropped,
            dict(bucket.labels),
            bucket.want_success,
            failure,
            changed,
        )
        bucket.received = 0
        bucket.dropped = 0
        bucket.labels = {}
        bucket.want_success = False
        bucket.flushed_mono = mono
        if snap.want_success:
            bucket.stored_success = True
            bucket.stored_failing = bool(failure)
        elif failure:
            bucket.stored_failing = True
        if snap.dropped > 0:
            bucket.drop_flushed_at = now
        return snap


def _flush(snap: _Snap) -> None:
    if (
        not snap.changed
        and not snap.failure
        and snap.received == 0
        and snap.dropped == 0
        and not snap.labels
    ):
        return
    labels = snap.labels or None
    try:
        if snap.failure and snap.want_success:
            # The success is real and the failure came after it. One then the
            # other, so the run of failures starts at one and the counts land
            # on the success.
            record_collected(
                snap.source_id,
                received=snap.received,
                dropped=snap.dropped,
                labels=labels,
            )
            record_failed(snap.source_id, snap.failure[0], snap.failure[1])
            return
        if snap.failure:
            record_failed(
                snap.source_id,
                snap.failure[0],
                snap.failure[1],
                dropped=snap.dropped,
                received=snap.received,
                labels=labels,
            )
            return
        if snap.want_success:
            record_collected(
                snap.source_id,
                received=snap.received,
                dropped=snap.dropped,
                labels=labels,
            )
            return
        record_dropped(snap.source_id, snap.dropped, labels=labels)
    except Exception as exc:  # noqa: BLE001 - health never fails ingestion
        logger.warning(
            "Health write for %s failed: %s", snap.source_id, type(exc).__name__
        )


def note_success(
    source_id: str,
    *,
    received: int = 0,
    dropped: int = 0,
    labels: Optional[Iterable[str]] = None,
) -> None:
    """An accepted push, or a Kafka connect or batch that succeeded."""
    snap = _take(
        source_id,
        success=True,
        received=received,
        dropped=dropped,
        labels=labels,
        failure=None,
    )
    if snap is not None:
        _flush(snap)


def note_dropped(
    source_id: str,
    count: int = 1,
    *,
    labels: Optional[Iterable[str]] = None,
) -> None:
    """Unreadable records after the sender had already authenticated."""
    snap = _take(
        source_id,
        success=False,
        received=0,
        dropped=count,
        labels=labels,
        failure=None,
    )
    if snap is not None:
        _flush(snap)


def note_failure(
    source_id: str,
    error: str,
    kind: str = "unknown",
    *,
    dropped: int = 0,
    received: int = 0,
) -> None:
    """A failed attempt. Written at once, so a run of failures can build up."""
    if kind not in _KINDS:
        kind = "unknown"
    snap = _take(
        source_id,
        success=False,
        received=received,
        dropped=dropped,
        labels=None,
        failure=(error or "failed", kind),
    )
    if snap is not None:
        _flush(snap)
