"""Generate the ``steps`` of ``recorded_red_run.json`` from ``execute_atomic``.

The fixture is the tool's own output against a stub runner with ``_now``
pinned, so its shape cannot drift from what a real run journals. Rewrite it
with ``python3 -m tests.unit.detections.fixtures.art_trace`` from the repo
root; ``test_reconstruct_run.py`` asserts the committed ``steps`` equal
:func:`recorded_steps`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import core.integrations.atomic_red_team.tool as art

FIXTURE = Path(__file__).with_name("recorded_red_run.json")
CONFIG = {"runner_path": "/opt/art-runner", "atomics_path": "/opt/atomics"}
ENVIRONMENT_ID = "range-1"

# (technique, hostname, started_at, ended_at) — one execute call per row.
RUN = [
    (
        "T1059.001",
        "ws01.corp.local",
        "2026-09-10T12:00:00+00:00",
        "2026-09-10T12:05:00+00:00",
    ),
    (
        "T1003.001",
        "dc01.corp.local",
        "2026-09-10T12:06:00+00:00",
        "2026-09-10T12:08:00+00:00",
    ),
    (
        "T1021.002",
        "ws01.corp.local",
        "2026-09-10T12:10:00+00:00",
        "2026-09-10T12:12:00+00:00",
    ),
    (
        "T1047",
        "ws02.corp.local",
        "2026-09-10T12:20:00+00:00",
        "2026-09-10T12:22:00+00:00",
    ),
]


def stub_run(argv, **_kwargs):
    technique = argv[argv.index(art._TECHNIQUE_FLAG) + 1]
    return SimpleNamespace(
        returncode=0, stdout=f"[+] {technique} executed\n", stderr=""
    )


def recorded_steps() -> List[Dict[str, Any]]:
    clock = iter(stamp for row in RUN for stamp in row[2:])
    original_now = art._now
    art._now = lambda: next(clock)
    try:
        steps = [
            art.execute_atomic(
                {"technique": tid, "environment_id": ENVIRONMENT_ID, "hostname": host},
                CONFIG,
                run=stub_run,
            )
            for tid, host, _start, _end in RUN
        ]
    finally:
        art._now = original_now
    # Two _now() calls per step; a leftover stamp means the tool's clock use moved.
    assert next(clock, None) is None
    return steps


if __name__ == "__main__":
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recorded["steps"] = recorded_steps()
    FIXTURE.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
