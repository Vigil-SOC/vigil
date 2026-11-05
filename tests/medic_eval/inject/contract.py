"""The injector contract: a Cell is one fault with inject / verify / revert.

- `inject(stack)`: apply the fault. A second call is a no-op.
- `verify(stack, faulted)`: read Vigil directly (an HTTP response, a container's
  environment, the backend API, a log line), never Medic, and raise
  AssertionError unless the fault has taken effect (`faulted`) or is gone.
- `revert(stack)`: undo it. A second call is clean.

The label fields the cell writes sit beside them. `run_cell` runs one cycle and
writes `labels.json` in services/medic/contracts/trace-labels.schema.json's
shape, checked with trace_check's own validator rather than a second schema.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.medic.contracts.trace_check import LABELS, _check_labels

CLASSES = {"I": "ingest", "L": "llm", "P": "pipeline"}


@dataclass(frozen=True)
class Cell:
    cause: str  # fault.causes id, e.g. "I-5.a"
    lane: int  # lane the scorer expects
    detect_within_s: (
        int  # a rule's `for:`; see config_cells for cells with no merged rule
    )
    inject: Callable
    verify: Callable
    revert: Callable
    actions: tuple[str, ...]  # what inject did, for injection.actions
    undo: tuple[str, ...]  # what revert did, for recovery_action.actions
    may_fire: tuple[str, ...] = ()
    # For a cause whose rule's mode is not its prefix (I-1.g is mode I-2).
    mode_override: str | None = None

    @property
    def mode(self) -> str:
        return self.mode_override or self.cause.split(".")[0]


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def labels_for(cell: Cell, at: dict[str, datetime]) -> dict:
    """labels.json for one run. `at` holds start, inject, recover, end."""
    fault_s = int((at["recover"] - at["inject"]).total_seconds())
    window = lambda a, b: {"start": _ts(at[a]), "end": _ts(at[b])}  # noqa: E731
    expect = lambda fires, may, detect: {  # noqa: E731
        "fires": fires,
        "may_fire": may,
        "detect_within_s": detect,
        "clear_within_s": None,
    }
    return {
        "fault": {
            "mode": cell.mode,
            "cause": cell.cause,
            "class": CLASSES[cell.mode[0]],
            "lane_expected": cell.lane,
        },
        "windows": {
            "baseline": window("start", "inject"),
            "fault": window("inject", "recover"),
            "recovery": window("recover", "end"),
        },
        "injection": {"at": _ts(at["inject"]), "actions": list(cell.actions)},
        "recovery_action": {"at": _ts(at["recover"]), "actions": list(cell.undo)},
        "expected": {
            "baseline": expect([], [], None),
            # The window is as long as the fault was held; a deadline past it is
            # not a valid label. Hold the fault for detect_within_s to score it whole.
            "fault": expect(
                [cell.mode], list(cell.may_fire), min(cell.detect_within_s, fault_s)
            ),
            "recovery": expect([], [cell.mode], None),
        },
    }


def check_labels(labels: dict, trace_id: str) -> list[str]:
    """Schema and cross-field violations; empty means valid."""
    errors = [e.message for e in LABELS.iter_errors(labels)]
    if errors:
        return errors
    windows = labels["windows"]
    start = datetime.fromisoformat(windows["baseline"]["start"])
    end = datetime.fromisoformat(windows["recovery"]["end"])
    return [
        f"{v.code}: {v.detail}" for v in _check_labels(labels, trace_id, start, end)
    ]


def run_cell(
    cell: Cell, stack, out: Path, hold_s: float = 0, baseline_s: float = 2
) -> dict:
    """inject -> verify -> revert -> verify-clean once; write and return labels.

    inject and revert each run twice to prove they are idempotent. The fault
    window runs from the moment inject returns (the fault is then in effect) to
    the moment revert returns (it is gone). `baseline_s` is the quiet time
    before inject; `hold_s` keeps the fault in place before reverting (a scoring
    run holds it for detect_within_s). A failed check still reverts, so one
    broken cell does not break the ones after it.
    """
    at = {"start": _now()}
    time.sleep(baseline_s)
    try:
        cell.inject(stack)
        at["inject"] = _now()
        cell.inject(stack)
        cell.verify(stack, True)
        time.sleep(hold_s)
    finally:
        cell.revert(stack)
        at.setdefault("inject", _now())
        at["recover"] = _now()
    cell.revert(stack)
    cell.verify(stack, False)
    at["end"] = _now()

    labels = labels_for(cell, at)
    trace_id = f"{cell.cause}-{at['inject'].strftime('%Y%m%dT%H%M%S%f')}"
    problems = check_labels(labels, trace_id)
    assert not problems, problems
    run_dir = out / trace_id
    run_dir.mkdir(parents=True)
    (run_dir / "labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    return labels
