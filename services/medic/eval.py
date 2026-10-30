"""Score injection labels against Medic decision records (I3).

``cells`` are ``{"instance_id", "labels"}``: a parsed labels document
(``trace-labels.schema.json``) and the Medic that ran it. ``records`` are
parsed decision-record objects. This module reads neither trace directories
nor ``GET /v1/decisions``.

Opens join on ``body.instance_id``, ``body.fault.mode`` and a half-open
window (``start <= at < end``). ``body.fault.causes`` is not a join key.
Resolves join on ``incident_id``. An incident that resolves ``closed_quietly``
is neither a fire nor a false alarm.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

ON_TIME: Literal["on_time"] = "on_time"
LATE: Literal["late"] = "late"
MISS: Literal["miss"] = "miss"

_WINDOWS = ("baseline", "fault", "recovery")
_LANES = (1, 2, 3)
_DETECTION_TYPES = ("event", "absence")
Outcome = Literal["on_time", "late", "miss"]


@dataclass(frozen=True)
class ModeScore:
    """One ``expected.fault.fires`` mode on one cell."""

    mode: str
    outcome: Outcome
    time_to_detect_s: float | None
    lane: int | None
    detection_type: str | None
    subject: str | None
    incident_id: str | None
    cleared: bool | None


@dataclass(frozen=True)
class FalseAlarm:
    incident_id: str
    mode: str | None
    window: str
    at: str


@dataclass(frozen=True)
class CellScore:
    instance_id: str
    injected: bool
    fault_mode: str | None
    lane_expected: int | None
    modes: tuple[ModeScore, ...]
    false_alarms: tuple[FalseAlarm, ...]
    # Open-in-window pairs judged for false alarms. Quiet closes are omitted.
    opens_in_windows: int


@dataclass(frozen=True)
class Rate:
    """On-time ``fault.mode`` over cells that have an injection.

    Late stays out of ``hits``. A wrong lane stays in. ``rate`` is ``hits / n``,
    or None when ``n`` is 0.
    """

    hits: int
    n: int
    rate: float | None


@dataclass(frozen=True)
class Median:
    """Median seconds. ``n`` is how many times went in; misses are omitted."""

    n: int
    median: float | None


@dataclass(frozen=True)
class LaneMatrix:
    """``fault.lane_expected`` by the detection open's ``lane.value``.

    Rows and columns are lanes 1, 2, 3, in that order. Misses, a null
    expected lane, and ``subject: watcher`` stay out. ``n`` counts entries.
    """

    cells: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]
    n: int


@dataclass(frozen=True)
class FalseAlarms:
    """``count`` is alarms. ``n`` is the open-in-window pairs that were judged."""

    count: int
    n: int


@dataclass(frozen=True)
class Metrics:
    detection_rate: Rate
    time_to_detect_s: Median
    time_to_detect_s_by_type: dict[str, Median]
    lane_matrix: LaneMatrix
    false_alarms: FalseAlarms


@dataclass(frozen=True)
class Run:
    cells: tuple[CellScore, ...]
    metrics: Metrics


@dataclass(frozen=True)
class _Open:
    index: int
    at: datetime
    at_text: str
    incident_id: str
    instance_id: str
    mode: str | None
    lane: int | None
    detection_type: str | None
    subject: str | None


def score(
    cells: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> Run:
    """Per-cell results and the run metrics for ``cells`` against ``records``."""
    opens, quiet, resolved_at = _records(records)
    live = tuple(o for o in opens if o.incident_id not in quiet)
    scored = tuple(_cell(cell, live, resolved_at) for cell in cells)
    return Run(cells=scored, metrics=_metrics(scored))


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[tuple[_Open, ...], set[str], dict[str, datetime]]:
    opens: list[_Open] = []
    quiet: set[str] = set()
    resolved_at: dict[str, datetime] = {}
    for index, rec in enumerate(records):
        if not isinstance(rec, Mapping):
            continue
        body = rec.get("body")
        if not isinstance(body, Mapping):
            continue
        kind = rec.get("type")
        if kind == "incident_resolved":
            _resolve(rec, body, quiet, resolved_at)
        elif kind == "incident_opened":
            opened = _open(index, rec, body)
            if opened is not None:
                opens.append(opened)
    return tuple(opens), quiet, resolved_at


def _resolve(
    rec: Mapping[str, Any],
    body: Mapping[str, Any],
    quiet: set[str],
    resolved_at: dict[str, datetime],
) -> None:
    incident_id = body.get("incident_id")
    if not isinstance(incident_id, str):
        return
    # Any quiet close drops the incident, even if another resolve says cleared.
    if body.get("how") == "closed_quietly":
        quiet.add(incident_id)
        return
    at = _instant(rec["at"])
    prev = resolved_at.get(incident_id)
    if prev is None or at < prev:
        resolved_at[incident_id] = at


def _open(index: int, rec: Mapping[str, Any], body: Mapping[str, Any]) -> _Open | None:
    incident_id = body.get("incident_id")
    instance_id = body.get("instance_id")
    at_text = rec.get("at")
    if not isinstance(incident_id, str) or not isinstance(instance_id, str):
        return None
    if not isinstance(at_text, str):
        return None
    return _Open(
        index=index,
        at=_instant(at_text),
        at_text=at_text,
        incident_id=incident_id,
        instance_id=instance_id,
        mode=_mode(body),
        lane=_lane(body),
        detection_type=_text(body.get("detection_type")),
        subject=_text(body.get("subject")),
    )


def _mode(body: Mapping[str, Any]) -> str | None:
    """``fault.mode`` only. ``fault.causes`` is a different id and must not match."""
    fault = body.get("fault")
    if not isinstance(fault, Mapping):
        return None
    return _text(fault.get("mode"))


def _lane(body: Mapping[str, Any]) -> int | None:
    lane = body.get("lane")
    if not isinstance(lane, Mapping):
        return None
    value = lane.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _cell(
    cell: Mapping[str, Any],
    opens: Sequence[_Open],
    resolved_at: Mapping[str, datetime],
) -> CellScore:
    labels = cell["labels"]
    instance_id = cell["instance_id"]
    if not isinstance(labels, Mapping) or not isinstance(instance_id, str):
        raise TypeError("each cell needs a string instance_id and a labels object")
    fault_mode, lane_expected = _fault(labels)
    injected = "injection" in labels
    injection_at = _injection_at(labels)
    fault_span = _span(labels, "fault")
    recovery_start = _recovery_start(labels)
    detect_within = _seconds(_expectation(labels, "fault"), "detect_within_s")
    clear_within = _seconds(_expectation(labels, "recovery"), "clear_within_s")
    mine = tuple(o for o in opens if o.instance_id == instance_id)
    modes = tuple(
        _mode_score(
            mode,
            _first(mine, mode, fault_span),
            fault_span,
            detect_within,
            clear_within,
            recovery_start,
            injection_at,
            resolved_at,
        )
        for mode in _fires(labels)
    )
    alarms, considered = _alarms(mine, labels)
    return CellScore(
        instance_id=instance_id,
        injected=injected,
        fault_mode=fault_mode,
        lane_expected=lane_expected,
        modes=modes,
        false_alarms=alarms,
        opens_in_windows=considered,
    )


def _fault(labels: Mapping[str, Any]) -> tuple[str | None, int | None]:
    fault = labels.get("fault")
    if not isinstance(fault, Mapping):
        return None, None
    lane = fault.get("lane_expected")
    if isinstance(lane, bool) or not isinstance(lane, int):
        lane = None
    return _text(fault.get("mode")), lane


def _injection_at(labels: Mapping[str, Any]) -> datetime | None:
    injection = labels.get("injection")
    if not isinstance(injection, Mapping):
        return None
    at = injection.get("at")
    return _instant(at) if isinstance(at, str) else None


def _expectation(labels: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    expected = labels.get("expected")
    if not isinstance(expected, Mapping):
        return {}
    exp = expected.get(name)
    return exp if isinstance(exp, Mapping) else {}


def _fires(labels: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _expectation(labels, "fault").get("fires") or []
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return ()
    return tuple(mode for mode in raw if isinstance(mode, str))


def _seconds(exp: Mapping[str, Any], key: str) -> float | None:
    value = exp.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _span(labels: Mapping[str, Any], name: str) -> tuple[datetime, datetime] | None:
    windows = labels.get("windows")
    if not isinstance(windows, Mapping):
        return None
    raw = windows.get(name)
    if not isinstance(raw, Mapping):
        return None
    start, end = raw.get("start"), raw.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    return _instant(start), _instant(end)


def _recovery_start(labels: Mapping[str, Any]) -> datetime | None:
    span = _span(labels, "recovery")
    return None if span is None else span[0]


def _first(
    opens: Sequence[_Open],
    mode: str,
    span: tuple[datetime, datetime] | None,
) -> _Open | None:
    if span is None:
        return None
    start, end = span
    found = [o for o in opens if o.mode == mode and start <= o.at < end]
    if not found:
        return None
    return min(found, key=lambda o: (o.at, o.index))


def _mode_score(
    mode: str,
    opened: _Open | None,
    fault_span: tuple[datetime, datetime] | None,
    detect_within: float | None,
    clear_within: float | None,
    recovery_start: datetime | None,
    injection_at: datetime | None,
    resolved_at: Mapping[str, datetime],
) -> ModeScore:
    outcome = _outcome(opened, fault_span, detect_within)
    return ModeScore(
        mode=mode,
        outcome=outcome,
        time_to_detect_s=_ttd(opened, outcome, injection_at),
        lane=None if opened is None else opened.lane,
        detection_type=None if opened is None else opened.detection_type,
        subject=None if opened is None else opened.subject,
        incident_id=None if opened is None else opened.incident_id,
        cleared=_cleared(opened, outcome, clear_within, recovery_start, resolved_at),
    )


def _outcome(
    opened: _Open | None,
    fault_span: tuple[datetime, datetime] | None,
    detect_within: float | None,
) -> Outcome:
    if opened is None or fault_span is None:
        return MISS
    # A null detect_within_s means any open in the fault window is on time.
    if detect_within is None:
        return ON_TIME
    deadline = fault_span[0] + timedelta(seconds=detect_within)
    return ON_TIME if opened.at <= deadline else LATE


def _ttd(
    opened: _Open | None,
    outcome: Outcome,
    injection_at: datetime | None,
) -> float | None:
    if opened is None or outcome == MISS or injection_at is None:
        return None
    return (opened.at - injection_at).total_seconds()


def _cleared(
    opened: _Open | None,
    outcome: Outcome,
    clear_within: float | None,
    recovery_start: datetime | None,
    resolved_at: Mapping[str, datetime],
) -> bool | None:
    # A null clear_within_s is no clear check. A miss never fired.
    if outcome == MISS or clear_within is None:
        return None
    if opened is None or recovery_start is None:
        return False
    at = resolved_at.get(opened.incident_id)
    if at is None:
        return False
    return at <= recovery_start + timedelta(seconds=clear_within)


def _alarms(
    opens: Sequence[_Open], labels: Mapping[str, Any]
) -> tuple[tuple[FalseAlarm, ...], int]:
    alarms: list[tuple[datetime, int, FalseAlarm]] = []
    considered = 0
    for opened in opens:
        for name, start, end, allowed in _allowed(labels):
            if start <= opened.at < end:
                considered += 1
                if opened.mode not in allowed:
                    alarms.append(
                        (
                            opened.at,
                            opened.index,
                            FalseAlarm(
                                incident_id=opened.incident_id,
                                mode=opened.mode,
                                window=name,
                                at=opened.at_text,
                            ),
                        )
                    )
    alarms.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in alarms), considered


def _allowed(
    labels: Mapping[str, Any],
) -> tuple[tuple[str, datetime, datetime, frozenset[str]], ...]:
    out = []
    for name in _WINDOWS:
        span = _span(labels, name)
        if span is None:
            continue
        exp = _expectation(labels, name)
        out.append((name, span[0], span[1], _allowed_modes(exp)))
    return tuple(out)


def _allowed_modes(exp: Mapping[str, Any]) -> frozenset[str]:
    modes: list[str] = []
    for key in ("fires", "may_fire"):
        raw = exp.get(key) or []
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            modes.extend(mode for mode in raw if isinstance(mode, str))
    return frozenset(modes)


def _metrics(cells: Sequence[CellScore]) -> Metrics:
    hits = 0
    injected = 0
    samples: list[tuple[float, str | None]] = []
    matrix = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    lane_n = 0
    alarm_count = 0
    alarm_n = 0
    for cell in cells:
        alarm_count += len(cell.false_alarms)
        alarm_n += cell.opens_in_windows
        detected = _detection(cell)
        if cell.injected:
            injected += 1
            if detected is not None and detected.outcome == ON_TIME:
                hits += 1
        if detected is None or detected.outcome == MISS:
            continue
        if cell.injected and detected.time_to_detect_s is not None:
            samples.append((detected.time_to_detect_s, detected.detection_type))
        lane_n += _tally(matrix, cell.lane_expected, detected)
    return Metrics(
        detection_rate=_rate(hits, injected),
        time_to_detect_s=_median([v for v, _ in samples]),
        time_to_detect_s_by_type=_by_type(samples),
        lane_matrix=LaneMatrix(
            cells=(
                (matrix[0][0], matrix[0][1], matrix[0][2]),
                (matrix[1][0], matrix[1][1], matrix[1][2]),
                (matrix[2][0], matrix[2][1], matrix[2][2]),
            ),
            n=lane_n,
        ),
        false_alarms=FalseAlarms(count=alarm_count, n=alarm_n),
    )


def _detection(cell: CellScore) -> ModeScore | None:
    """The cell's ``fault.mode`` score. Other fired modes stay out of run metrics."""
    if cell.fault_mode is None:
        return None
    for mode in cell.modes:
        if mode.mode == cell.fault_mode:
            return mode
    return None


def _tally(
    matrix: list[list[int]],
    expected: int | None,
    detected: ModeScore,
) -> int:
    if detected.subject == "watcher":
        return 0
    if expected not in _LANES or detected.lane not in _LANES:
        return 0
    matrix[expected - 1][detected.lane - 1] += 1
    return 1


def _rate(hits: int, n: int) -> Rate:
    return Rate(hits=hits, n=n, rate=None if n == 0 else hits / n)


def _median(values: Sequence[float]) -> Median:
    if not values:
        return Median(n=0, median=None)
    return Median(n=len(values), median=float(statistics.median(values)))


def _by_type(samples: Sequence[tuple[float, str | None]]) -> dict[str, Median]:
    kinds = list(_DETECTION_TYPES)
    kinds.extend(
        sorted(
            {
                kind
                for _, kind in samples
                if kind not in _DETECTION_TYPES and kind is not None
            }
        )
    )
    return {
        kind: _median([value for value, sample_kind in samples if sample_kind == kind])
        for kind in kinds
    }
