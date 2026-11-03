"""Medic replay (E6): a trace through the engine on a fake clock, into a fresh store.

  python -m services.medic.replay <trace-dir> --store <dir>

The trace is validated first (`contracts/trace_check.py`). Its observations are fed
at their `arrived` times through the app's own path (`Medic.sink`, `Medic.evaluate`:
engine, router, store writer) on the 15 s grid, from `capture.started_at` to
`capture.ended_at`, empty ticks included (silence and staleness rules fire on them).
Observations were captured after redaction, so replay runs no sensor and no redactor.

The store runs under the trace's `instance_id` and holds engine records only
(decision-record.md §3). Nothing reads the wall clock, so the same trace gives
byte-identical store files. Replay never touches a store that already holds anything.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.medic.app.trace import pack_of
from services.medic.app.wiring import TICK_S, Medic, load_pack
from services.medic.contracts.trace_check import validate
from services.medic.store import StoreError, open_writer
from services.medic.store.files import INSTANCE_NAME, create_private_file


class ReplayClock:
    """The `Clock` protocol (sensors/base.py), set by the replay loop to the tick."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def wall(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:  # no sensor runs: nothing waits
        return None


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _file_lines(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """(grid tick the line arrived in, observation). A line that arrived between
    ticks is seen by the tick it falls in, as it would have been live."""
    with path.open("rb") as f:
        for raw in f:
            line = json.loads(raw)
            yield (
                math.floor(_epoch(line["arrived"]) / TICK_S) * TICK_S,
                line["observation"],
            )


def batches(
    trace_dir: Path, first: int, last: int
) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """Every 15 s tick in [first, last] with the observations that arrived in it.
    Within a tick: by sensor file name, then capture order (one file's own order is
    all a trace keeps)."""
    paths = sorted((trace_dir / "observations").glob("*.jsonl"))
    lines = heapq.merge(*map(_file_lines, paths), key=lambda item: item[0])
    pending = next(lines, None)
    for tick in range(first, last + 1, TICK_S):
        batch = []
        while pending is not None and pending[0] <= tick:
            batch.append(pending[1])
            pending = next(lines, None)
        yield tick, batch


def tick_range(manifest: dict[str, Any]) -> tuple[int, int]:
    cap = manifest["capture"]
    first = math.floor(_epoch(cap["started_at"]) / TICK_S) * TICK_S
    return first, int(_epoch(cap["ended_at"]))


def drive(medic: Medic, clock: ReplayClock, ticks: Iterator) -> None:
    """Each tick: its observations into the engine's sink, then evaluate."""
    for tick, batch in ticks:
        clock.now = tick
        for observation in batch:
            medic.sink.write(observation)
        medic.evaluate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.medic.replay")
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args(argv)
    trace_dir, store = args.trace_dir, args.store

    problems = validate(trace_dir)
    if problems:
        for p in problems:
            print(f"{p.code}: {p.detail}", file=sys.stderr)
        print(f"replay refused: {trace_dir} is not a valid trace", file=sys.stderr)
        return 2
    manifest = json.loads((trace_dir / "manifest.json").read_text("utf-8"))
    medic_block = manifest["medic"]
    shape = medic_block["install_shape"]

    # One rule covers a live Medic, a stopped one and an old replay store.
    if store.exists() and (not store.is_dir() or any(store.iterdir())):
        print(
            f"replay refused: --store {store} already holds something", file=sys.stderr
        )
        return 2

    first, last = tick_range(manifest)
    loaded = load_pack(
        shape,
        dev_mode=True,  # the source tree's pack; non-dev would write pack state into the store
        data_dir=store,
        now=datetime.fromtimestamp(_epoch(manifest["capture"]["started_at"]), UTC),
    )
    pack = pack_of(loaded.rules)
    traced = (medic_block["pack"]["id"], medic_block["pack"]["version"])
    if traced != pack:
        print(
            f"warning: the trace ran pack {'/'.join(traced)}, replay loads "
            f"{'/'.join(pack)}",
            file=sys.stderr,
        )

    # The store runs under the trace's instance id: the file goes in before the writer.
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    id_file = store / INSTANCE_NAME
    create_private_file(id_file)
    id_file.write_text(medic_block["instance_id"] + "\n")

    clock = ReplayClock(first)
    try:
        with open_writer(store, shape=shape) as writer:
            medic = Medic(
                writer=writer,
                rules=loaded.rules,
                sensors=[],
                clock=clock,
                shape=shape,
                suppression=loaded.suppression,
            )
            drive(medic, clock, batches(trace_dir, first, last))
            report = writer.verify()
            unstored, held = medic.unstored, len(medic.held)
    except StoreError as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1

    print(f"replay_head: seq {report.head_seq} hash {report.head_hash}")
    print(f"records: {report.count}")
    print(f"pack: {'/'.join(pack)} ({len(loaded.rules)} rules)")
    print(f"rules: {', '.join(sorted(r.id for r in loaded.rules))}")
    print(
        f"ticks: {_iso(first)} .. {_iso(last - last % TICK_S)} ({(last - first) // TICK_S + 1})"
    )
    if unstored or held:
        # A disk-floor-dependent partial replay must not pass as a good one.
        print(f"replay incomplete: {unstored} unstored, {held} held", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
