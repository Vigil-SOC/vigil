"""What Vigil learned in a window (#906, #907).

``recall_entity`` answers by entity and the Ledger by run; neither answers "what
did we learn last month". The Distil already materialised every conclusion a hunt
or Case reached, and stamped each investigation with a marker when it did. One
marker is one learning episode. A finished compose run whose projection carries
an execute trace is one too: it is scored when listed or exported, from Findings
and that trace, and is not a Distil row. This module lists both in a time window
and writes a chosen subset to a JSONL file under the State Directory. No table,
no route, no daemon job.

A Distil episode is an envelope off the marker -- ``kind``, ``investigation_id``,
``origin_run_id`` (null on a Case), ``concluded_at`` -- with the investigation's
Verdicts and Gaps as ``payload``, in the same shape ``recall_entity`` returns
them. A marker with neither is still an episode: concluding nothing is a
conclusion. An emulation episode uses the same envelope with ``kind``
``emulation``, both ids the run id, and the coverage report as ``payload``.

Export redacts by default. ``identified=False`` keeps every entity's *type* and
drops its value (``ip:10.0.0.7`` becomes ``ip:*``), on Verdicts and Gaps alike,
so the file says what kinds of things were concluded about without naming the
customer's hosts. Statements, rationale, outcomes and stances are never touched.
On an emulation episode the same flag drops host, ip, user, and command off
missed steps and leaves technique ids, verdicts, and citation text.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from core.agents.projections import parse_window_instant, read_projection
from core.config import vigil_path
from core.detections.reconstruction import (
    _EVIDENCE_KEYS,
    coverage_report,
    steps_from_dispatch_results,
)
from core.detections.tools import get_security_detection_tools
from core.memory.recall import _gap, _iso, _verdict
from core.storage.models.episodic import (
    EpisodicDistilMarker,
    EpisodicGap,
    EpisodicVerdict,
    EpisodicVerdictSource,
)
from core.storage.unit_of_work import unit_of_work
from core.workflows.workflow_run_service import LIST_RUNS_MAX, WorkflowRunService

Args = Dict[str, Any]
Pair = Tuple[str, str]

LIST_TOOL = "list_learning_episodes"
EXPORT_TOOL = "export_learning_episodes"

# Marker query only. Emulation is scored from a compose run at read time, so it
# is not a Distil kind. ``analyst`` markers are not investigations a customer
# asked about.
EPISODE_KINDS: Tuple[str, ...] = ("hunt", "case")
_EXPORT_KINDS: Tuple[str, ...] = EPISODE_KINDS + ("emulation",)
# Cancelled compose runs are not episodes. Failed ones are: the trace still ran.
_COMPOSE_STATUSES: Tuple[str, ...] = ("completed", "failed")
DEFAULT_LIMIT = 200

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _refuse(tool: str, detail: str) -> TypeError:
    # Worded so tools_router reads it as invalid_args rather than backend_error.
    return TypeError(f"{tool}: {detail} (required keyword-only argument)")


def _bound(tool: str, value: Any, *, end: bool) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _refuse(tool, "start and end are required ISO-8601 timestamps")
    try:
        parsed = parse_window_instant(value, end=end)
    except ValueError:
        raise _refuse(tool, f"{value!r} is not an ISO-8601 timestamp") from None
    # parse_window_instant answers naive UTC to match workflow_runs; the episodic
    # columns are timestamptz, so the offset goes back on.
    return parsed.replace(tzinfo=timezone.utc)


def _envelope(marker: EpisodicDistilMarker) -> Dict[str, Any]:
    return {
        "kind": marker.investigation_kind,
        "investigation_id": marker.investigation_id,
        "origin_run_id": (
            str(marker.origin_run_id) if marker.origin_run_id is not None else None
        ),
        "concluded_at": _iso(marker.concluded_at),
        "payload": {"verdicts": [], "gaps": []},
    }


def _payloads(db: Session, episodes: Mapping[Pair, Dict[str, Any]]) -> None:
    """Fill each envelope's payload from the Verdict and Gap tables in place."""
    if not episodes:
        return
    pairs = list(episodes)
    verdicts_by_id: Dict[int, Dict[str, Any]] = {}
    for table, build, bucket in (
        (EpisodicVerdict.__table__, _verdict, "verdicts"),
        (EpisodicGap.__table__, _gap, "gaps"),
    ):
        stmt = (
            select(table)
            .where(
                tuple_(table.c.investigation_kind, table.c.investigation_id).in_(pairs)
            )
            .order_by(table.c.hypothesis_id, table.c.id)
        )
        for row in db.execute(stmt).mappings():
            built = build(row)
            key = (row["investigation_kind"], row["investigation_id"])
            episodes[key]["payload"][bucket].append(built)
            if bucket == "verdicts":
                verdicts_by_id[row["id"]] = built
    if not verdicts_by_id:
        return
    sources = EpisodicVerdictSource.__table__
    stmt = (
        select(sources)
        .where(sources.c.verdict_id.in_(list(verdicts_by_id)))
        .order_by(sources.c.verdict_id, sources.c.source_system)
    )
    for row in db.execute(stmt).mappings():
        verdicts_by_id[row["verdict_id"]]["sources"].append(
            {
                "source_system": row["source_system"],
                "stance": row["stance"],
                "source_tier": row["source_tier"],
            }
        )


def _episodes(
    db: Session, markers: Iterable[EpisodicDistilMarker]
) -> List[Dict[str, Any]]:
    by_pair = {
        (m.investigation_kind, m.investigation_id): _envelope(m) for m in markers
    }
    _payloads(db, by_pair)
    return list(by_pair.values())


def _instant(value: Any) -> datetime:
    """UTC instant for a marker stamp or a run ``finished_at``.

    ``dump_summary`` emits ``+00:00`` and markers emit ``Z``, and a whole second
    omits the fraction. Those strings do not sort in time order.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    else:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp(value: Any) -> Any:
    if not isinstance(value, (datetime, str)) or value == "":
        return None
    return _iso(_instant(value))


def _distil_page(
    start: datetime,
    end: datetime,
    limit: int,
    session: Optional[Session],
) -> Tuple[List[Dict[str, Any]], int]:
    marker = EpisodicDistilMarker
    window = (
        marker.investigation_kind.in_(EPISODE_KINDS),
        marker.concluded_at >= start,
        marker.concluded_at <= end,
    )
    with unit_of_work(session) as db:
        total = db.scalar(select(func.count()).select_from(marker).where(*window)) or 0
        markers = db.scalars(
            select(marker)
            .where(*window)
            .order_by(
                marker.concluded_at.desc(),
                marker.investigation_kind,
                marker.investigation_id,
            )
            .limit(limit)
        ).all()
        return _episodes(db, markers), int(total)


def _compose_runs(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    service = WorkflowRunService()
    runs: List[Dict[str, Any]] = []
    seen = set()
    for status in _COMPOSE_STATUSES:
        for run in service.list_runs(
            run_kind="compose",
            status=status,
            finished_after=start,
            finished_at=end,
            limit=LIST_RUNS_MAX,
        ):
            run_id = run.get("run_id")
            if not run_id or run_id in seen:
                continue
            seen.add(run_id)
            runs.append(run)
    return runs


async def _score(run: Mapping[str, Any], projection: Any) -> Optional[Dict[str, Any]]:
    """One projection read, then the coverage path. An empty trace is absent."""
    if projection is None:
        return None
    trace = steps_from_dispatch_results(projection)
    if not trace:
        return None
    run_id = str(run["run_id"])
    reconstructed = await get_security_detection_tools().reconstruct_run(steps=trace)
    return {
        "kind": "emulation",
        "investigation_id": run_id,
        "origin_run_id": run_id,
        "concluded_at": _stamp(run.get("finished_at")),
        "payload": coverage_report(trace, reconstructed),
    }


async def _emulation_episodes(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    runs = _compose_runs(start, end)
    projections = await asyncio.gather(*(read_projection(r["run_id"]) for r in runs))
    episodes: List[Dict[str, Any]] = []
    for run, projection in zip(runs, projections):
        episode = await _score(run, projection)
        if episode is not None:
            episodes.append(episode)
    return episodes


def _newest(episodes: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    # Stable: concluded_at descending, then kind, then id. The marker query
    # uses that order, and the merge has to agree or a tie flips the page.
    episodes.sort(key=lambda episode: (episode["kind"], episode["investigation_id"]))
    episodes.sort(
        key=lambda episode: _instant(episode.get("concluded_at")), reverse=True
    )
    return episodes[:limit]


async def list_episodes(
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_LIMIT,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Hunt, Case, and emulation episodes that concluded in [start, end].

    Newest first. ``total`` counts every match so a caller shown ``limit``
    episodes can tell a quiet month from a truncated one. A compose run with
    no execute trace, or whose projection cannot be read, is not a match.
    """
    if start > end:
        raise _refuse(LIST_TOOL, "start must be at or before end")
    distil, marker_total = _distil_page(start, end, limit, session)
    emulations = await _emulation_episodes(start, end)
    # The newest ``limit`` markers plus every emulation in the window is enough
    # for the merged page: any marker older than that page is older than
    # ``limit`` markers, so it cannot enter the top of the union.
    episodes = _newest(distil + emulations, limit)
    total = marker_total + len(emulations)
    return {
        "start": _iso(start),
        "end": _iso(end),
        "episodes": episodes,
        "total": total,
        "dropped": max(total - len(episodes), 0),
    }


def _redact_key(key: str) -> str:
    kind, sep, _value = key.partition(":")
    return f"{kind}:*" if sep else "*"


def redact(episode: Dict[str, Any]) -> Dict[str, Any]:
    """The episode with every subject entity reduced to its type."""
    payload = {
        bucket: [
            {
                **row,
                "subject_entities": [_redact_key(k) for k in row["subject_entities"]],
            }
            for row in rows
        ]
        for bucket, rows in episode["payload"].items()
    }
    return {**episode, "payload": payload}


def _selection(tool: str, raw: Any) -> List[Pair]:
    """``[{kind, investigation_id}]`` to pairs; order kept, duplicates dropped."""
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise _refuse(tool, "episodes must be a list of {kind, investigation_id}")
    pairs: List[Pair] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _refuse(tool, "episodes must be a list of {kind, investigation_id}")
        kind, ident = item.get("kind"), item.get("investigation_id")
        if kind not in _EXPORT_KINDS or not ident:
            raise _refuse(
                tool,
                f"each episode needs kind in {list(_EXPORT_KINDS)} and investigation_id",
            )
        pair = (str(kind), str(ident))
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _export_path(name: Optional[str]) -> Path:
    stem = _SAFE_NAME.sub("_", name or "").strip("._-")
    if not stem:
        stem = "learning_episodes_" + datetime.now(timezone.utc).strftime(
            "%Y%m%d_%H%M%S"
        )
    return vigil_path("exports", f"{stem}.jsonl", write=True)


def _marker_episodes(
    pairs: Sequence[Pair], session: Optional[Session]
) -> Dict[Pair, Dict[str, Any]]:
    marker = EpisodicDistilMarker
    with unit_of_work(session) as db:
        markers = db.scalars(
            select(marker).where(
                tuple_(marker.investigation_kind, marker.investigation_id).in_(pairs)
            )
        ).all()
        by_pair = {(m.investigation_kind, m.investigation_id): m for m in markers}
        episodes = _episodes(db, [by_pair[p] for p in pairs if p in by_pair])
    return {(e["kind"], e["investigation_id"]): e for e in episodes}


def _compose_terminal(run: Optional[Mapping[str, Any]]) -> bool:
    if not run or run.get("status") not in _COMPOSE_STATUSES:
        return False
    return (run.get("trigger_context") or {}).get("run_kind") == "compose"


async def _emulation_by_id(run_id: str) -> Optional[Dict[str, Any]]:
    run = WorkflowRunService().get_run(run_id)
    if not isinstance(run, Mapping) or not _compose_terminal(run):
        return None
    return await _score(run, await read_projection(run_id))


def _strip_emulation(episode: Dict[str, Any]) -> Dict[str, Any]:
    """Drop host, ip, user, and command from missed steps. Citations stay."""
    techniques = []
    for technique in episode["payload"].get("techniques") or []:
        if not isinstance(technique, dict):
            techniques.append(technique)
            continue
        missed = [
            (
                {key: value for key, value in step.items() if key not in _EVIDENCE_KEYS}
                if isinstance(step, dict)
                else step
            )
            for step in technique.get("missed") or []
        ]
        techniques.append({**technique, "missed": missed})
    return {**episode, "payload": {**episode["payload"], "techniques": techniques}}


def _for_export(episode: Dict[str, Any], *, identified: bool) -> Dict[str, Any]:
    if identified:
        return episode
    if episode["kind"] == "emulation":
        return _strip_emulation(episode)
    return redact(episode)


async def export_episodes(
    selection: Sequence[Pair],
    *,
    identified: bool = False,
    name: Optional[str] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Write the selected episodes as JSONL under the State Directory.

    Selection is ``(kind, investigation_id)``: a hunt and a Case can share an
    id. An emulation id is scored again from that run's projection. Pairs that
    name nothing are reported, not invented. An empty result writes nothing
    and answers ``path: null``.
    """
    pairs = list(selection)
    by_pair: Dict[Pair, Dict[str, Any]] = {}
    marker_pairs = [pair for pair in pairs if pair[0] in EPISODE_KINDS]
    if marker_pairs:
        by_pair.update(_marker_episodes(marker_pairs, session))
    for kind, ident in pairs:
        if kind != "emulation":
            continue
        episode = await _emulation_by_id(ident)
        if episode is not None:
            by_pair[(kind, ident)] = episode
    # Back in the order the caller asked for.
    episodes = [by_pair[pair] for pair in pairs if pair in by_pair]
    found = set(by_pair)
    missing = [
        {"kind": k, "investigation_id": i} for k, i in pairs if (k, i) not in found
    ]
    if not episodes:
        return {
            "path": None,
            "written": 0,
            "identified": identified,
            "missing": missing,
        }

    episodes = [_for_export(episode, identified=identified) for episode in episodes]
    path = _export_path(name)
    with path.open("w", encoding="utf-8") as handle:
        for episode in episodes:
            handle.write(json.dumps(episode, default=str) + "\n")
    return {
        "path": str(path),
        "written": len(episodes),
        "identified": identified,
        "missing": missing,
    }


async def list_learning_episodes(args: Args) -> Dict[str, Any]:
    """The backend tool: ``start``/``end`` window, optional ``limit``."""
    supplied = dict(args or {})
    start = _bound(LIST_TOOL, supplied.get("start"), end=False)
    end = _bound(LIST_TOOL, supplied.get("end"), end=True)
    limit = supplied.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or limit < 1:
        raise _refuse(LIST_TOOL, "limit must be a positive integer")
    return await list_episodes(start, end, limit=limit)


async def export_learning_episodes(args: Args) -> Dict[str, Any]:
    """The backend tool: ``episodes`` pairs, ``identified`` flag, optional ``name``.

    ``limit`` is what tools_router injects into every call and means nothing
    here; a selection is not a page.
    """
    supplied = dict(args or {})
    supplied.pop("limit", None)
    pairs = _selection(EXPORT_TOOL, supplied.get("episodes"))
    identified = supplied.get("identified", False)
    if not isinstance(identified, bool):
        raise _refuse(EXPORT_TOOL, "identified must be a boolean")
    name = supplied.get("name")
    if name is not None and not isinstance(name, str):
        raise _refuse(EXPORT_TOOL, "name must be a string")
    return await export_episodes(pairs, identified=identified, name=name)
