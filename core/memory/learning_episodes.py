"""What Vigil learned in a window, as a fold over Distil (#906).

``recall_entity`` answers by entity and the Ledger by run; neither answers "what
did we learn last month". The Distil already materialised every conclusion a hunt
or Case reached, and stamped each investigation with a marker when it did. One
marker is one learning episode. This module lists those in a time window and
writes a chosen subset to a JSONL file under the State Directory. It is a read
over ``episodic_distil_markers``, ``episodic_verdicts`` and ``episodic_gaps`` and
nothing else: no table, no route, no daemon job.

An episode is an envelope off the marker -- ``kind``, ``investigation_id``,
``origin_run_id`` (null on a Case), ``concluded_at`` -- with the investigation's
Verdicts and Gaps as ``payload``, in the same shape ``recall_entity`` returns
them. A marker with neither is still an episode: concluding nothing is a
conclusion.

Export redacts by default. ``identified=False`` keeps every entity's *type* and
drops its value (``ip:10.0.0.7`` becomes ``ip:*``), on Verdicts and Gaps alike,
so the file says what kinds of things were concluded about without naming the
customer's hosts. Statements, rationale, outcomes and stances are never touched.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from core.agents.projections import parse_window_instant
from core.config import vigil_path
from core.memory.recall import _gap, _iso, _verdict
from core.storage.models.episodic import (
    EpisodicDistilMarker,
    EpisodicGap,
    EpisodicVerdict,
    EpisodicVerdictSource,
)
from core.storage.unit_of_work import unit_of_work

Args = Dict[str, Any]
Pair = Tuple[str, str]

LIST_TOOL = "list_learning_episodes"
EXPORT_TOOL = "export_learning_episodes"

# The only kinds this fold lists. Emulation is #907's sibling; ``analyst``
# markers are not investigations a customer asked about.
EPISODE_KINDS: Tuple[str, ...] = ("hunt", "case")
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


def list_episodes(
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_LIMIT,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Hunt and Case episodes whose ``concluded_at`` falls in [start, end].

    Newest first, ties broken on the marker key so a page is the same page on
    identical data. ``total`` counts every match so a caller shown ``limit``
    episodes can tell a quiet month from a truncated one.
    """
    if start > end:
        raise _refuse(LIST_TOOL, "start must be at or before end")
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
        episodes = _episodes(db, markers)
    return {
        "start": _iso(start),
        "end": _iso(end),
        "episodes": episodes,
        "total": int(total),
        "dropped": max(int(total) - len(episodes), 0),
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
        if kind not in EPISODE_KINDS or not ident:
            raise _refuse(
                tool,
                f"each episode needs kind in {list(EPISODE_KINDS)} and investigation_id",
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


def export_episodes(
    selection: Sequence[Pair],
    *,
    identified: bool = False,
    name: Optional[str] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Write the selected episodes as JSONL under the State Directory.

    Selection is by marker key, ``(kind, investigation_id)``: a hunt and a Case
    can share an id. Pairs that name no marker are reported, not invented. An
    empty result writes nothing and answers ``path: null``.
    """
    pairs = list(selection)
    episodes: List[Dict[str, Any]] = []
    if pairs:
        marker = EpisodicDistilMarker
        with unit_of_work(session) as db:
            markers = db.scalars(
                select(marker).where(
                    tuple_(marker.investigation_kind, marker.investigation_id).in_(
                        pairs
                    )
                )
            ).all()
            by_pair = {(m.investigation_kind, m.investigation_id): m for m in markers}
            # Back in the order the caller asked for.
            episodes = _episodes(db, [by_pair[p] for p in pairs if p in by_pair])
    found = {(e["kind"], e["investigation_id"]) for e in episodes}
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

    if not identified:
        episodes = [redact(e) for e in episodes]
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


def list_learning_episodes(args: Args) -> Dict[str, Any]:
    """The backend tool: ``start``/``end`` window, optional ``limit``."""
    supplied = dict(args or {})
    start = _bound(LIST_TOOL, supplied.get("start"), end=False)
    end = _bound(LIST_TOOL, supplied.get("end"), end=True)
    limit = supplied.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or limit < 1:
        raise _refuse(LIST_TOOL, "limit must be a positive integer")
    return list_episodes(start, end, limit=limit)


def export_learning_episodes(args: Args) -> Dict[str, Any]:
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
    return export_episodes(pairs, identified=identified, name=name)
