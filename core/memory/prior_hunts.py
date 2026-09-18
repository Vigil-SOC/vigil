"""Prior hunts over a set of entity keys and ATT&CK techniques (#899).

``recall_entity`` is a capped, per-key read built for a worker mid-run, and
``GET /workflows/{id}/runs`` is unfiltered history. Neither answers the
operator's question "what has already hunted these keys or this technique, and
is anything hunting them now?" -- so this is a separate read (epic #886).

Concluded hunts come from ``episodic_verdicts``: a Verdict is a hit when its
``subject_entities`` overlaps the keys *or* its ``techniques`` overlaps the
T-IDs. A dimension the caller did not ask about adds no predicate, so a
key-only query never needs a technique match. ``origin_run_id`` comes from the
Distil marker of the same investigation, never from ``agent_events``.

In-flight hunts are ``workflow_runs`` in a non-terminal status, matched on what
the operator declared when starting them (``trigger_context``): the subjects
keyed by hypothesis statement, and a T-ID appearing in the hypothesis text. A
scheduled hunt that declared nothing is not a hit. The run's projection is not
read -- subjects are not on ``HypothesisStanding``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY as PGArray
from sqlalchemy.orm import Session

from core.agents.projections import THREAT_HUNT_WORKFLOW_ID
from core.memory.entity_keys import normalise_keys
from core.storage.unit_of_work import unit_of_work
from core.workflows.workflow_run_service import LIST_RUNS_MAX, WorkflowRunService

Row = Dict[str, Any]

# Exactly the statuses set_status accepts as non-terminal.
IN_FLIGHT_STATUSES = ("running", "paused")

_T_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")

# The same overlap idiom recall.py uses, which is what the GIN indexes answer.
_KEYS_OVERLAP = "v.subject_entities && CAST(:keys AS text[])"
_TECHNIQUES_OVERLAP = "v.techniques && CAST(:techniques AS text[])"

_ARRAY_PARAMS = (
    bindparam("keys", type_=PGArray(SAText)),
    bindparam("techniques", type_=PGArray(SAText)),
)


def _concluded_statement(predicate: str) -> Any:
    return text(f"""
        SELECT v.investigation_id, v.hypothesis_id, v.statement, v.outcome,
               v.concluded_at, m.origin_run_id,
               ARRAY(SELECT unnest(v.subject_entities)
                     INTERSECT SELECT unnest(CAST(:keys AS text[]))) AS matched_keys,
               ARRAY(SELECT unnest(v.techniques)
                     INTERSECT SELECT unnest(CAST(:techniques AS text[]))) AS matched_techniques
        FROM episodic_verdicts AS v
        LEFT JOIN episodic_distil_markers AS m
               ON m.investigation_kind = v.investigation_kind
              AND m.investigation_id = v.investigation_id
        WHERE v.investigation_kind = 'hunt' AND ({predicate})
        ORDER BY v.concluded_at DESC, v.id DESC
        LIMIT :cap
        """).bindparams(*_ARRAY_PARAMS)


# One statement per shape of question rather than a predicate built from
# strings at call time: the empty dimension must contribute no clause at all,
# not a clause against an empty array.
_CONCLUDED = {
    (True, False): _concluded_statement(_KEYS_OVERLAP),
    (False, True): _concluded_statement(_TECHNIQUES_OVERLAP),
    (True, True): _concluded_statement(f"{_KEYS_OVERLAP} OR {_TECHNIQUES_OVERLAP}"),
}


def normalise_techniques(techniques: Iterable[str]) -> List[str]:
    """Well-formed T-IDs, upper-cased and deduped in order; anything else dropped."""
    seen: List[str] = []
    for raw in techniques:
        candidate = str(raw).strip().upper()
        if _T_ID.match(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


def _iso(value: Any) -> Any:
    """ISO-8601 in UTC with ``Z``, whether the tier hands back a datetime or a
    string (``list_runs`` has already serialised its rows)."""
    if isinstance(value, datetime):
        value = (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    return value.replace("+00:00", "Z") if isinstance(value, str) else value


def _concluded(
    session: Session, keys: Sequence[str], techniques: Sequence[str]
) -> List[Row]:
    statement = _CONCLUDED[(bool(keys), bool(techniques))]
    params = {"keys": list(keys), "techniques": list(techniques), "cap": LIST_RUNS_MAX}
    return [
        {
            "investigation_id": row["investigation_id"],
            "hypothesis_id": row["hypothesis_id"],
            "statement": row["statement"],
            "outcome": row["outcome"],
            "concluded_at": _iso(row["concluded_at"]),
            "origin_run_id": (
                str(row["origin_run_id"]) if row["origin_run_id"] else None
            ),
            "matched_keys": sorted(row["matched_keys"] or []),
            "matched_techniques": sorted(row["matched_techniques"] or []),
        }
        for row in session.execute(statement, params).mappings()
    ]


def _declared_subjects(context: Mapping[str, Any]) -> List[str]:
    """Every key the operator declared, whichever statement it was keyed to.

    Stored as the caller sent it (``{statement: [keys]}``), so the shape is
    checked rather than trusted, and the keys are normalised the same way the
    query's were so a case difference cannot hide a match.
    """
    declared = context.get("hypothesis_subjects")
    if not isinstance(declared, dict):
        return []
    keys = [
        key
        for subjects in declared.values()
        if isinstance(subjects, list)
        for key in subjects
        if isinstance(key, str) and key.strip()
    ]
    return normalise_keys(keys)


def _in_flight_match(
    run: Mapping[str, Any], keys: Sequence[str], techniques: Sequence[str]
) -> Optional[Row]:
    context = run.get("trigger_context") or {}
    declared = set(_declared_subjects(context))
    matched_keys = sorted(key for key in keys if key in declared)

    hypothesis = context.get("hypothesis")
    if not isinstance(hypothesis, str):
        hypothesis = ""
    # Whole-token, so T1566 is not found inside T15661. A parent id still matches
    # a sub-technique in prose (T1071 in "T1071.001") -- a hypothesis is text the
    # operator wrote, not a cited list, and the concluded side's exact overlap
    # cannot be reproduced against it.
    matched_techniques = sorted(
        t
        for t in techniques
        if re.search(rf"\b{re.escape(t)}\b", hypothesis, re.IGNORECASE)
    )

    if not matched_keys and not matched_techniques:
        return None
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "started_at": _iso(run.get("started_at")),
        "hypothesis": hypothesis,
        "matched_keys": matched_keys,
        "matched_techniques": matched_techniques,
    }


def _in_flight(
    runs: WorkflowRunService, keys: Sequence[str], techniques: Sequence[str]
) -> List[Row]:
    hits: List[Row] = []
    for status in IN_FLIGHT_STATUSES:
        for run in runs.list_runs(
            workflow_id=THREAT_HUNT_WORKFLOW_ID, status=status, limit=LIST_RUNS_MAX
        ):
            hit = _in_flight_match(run, keys, techniques)
            if hit is not None:
                hits.append(hit)
    # One order across both statuses rather than two newest-first pages laid
    # end to end, and the same ceiling as the concluded half.
    hits.sort(key=lambda hit: (hit["started_at"] or "", hit["run_id"] or ""))
    hits.reverse()
    return hits[:LIST_RUNS_MAX]


def list_prior_hunts(
    entity_keys: Iterable[str],
    techniques: Iterable[str] = (),
    *,
    session: Optional[Session] = None,
    runs: Optional[WorkflowRunService] = None,
) -> Dict[str, Any]:
    """Concluded and in-flight hunts touching any of the keys or techniques.

    Keys and T-IDs are normalised here so a caller's spelling and the stored
    form agree. Nothing to ask about is an empty answer rather than an error,
    and nothing here is logged: this is an operator's listing, not a worker's
    recall, and the read log is the recall contract's.
    """
    keys = normalise_keys(list(entity_keys))
    t_ids = normalise_techniques(techniques)
    result: Dict[str, Any] = {
        "keys": keys,
        "techniques": t_ids,
        "concluded": [],
        "in_flight": [],
    }
    if not keys and not t_ids:
        return result

    with unit_of_work(session) as db:
        result["concluded"] = _concluded(db, keys, t_ids)
    result["in_flight"] = _in_flight(runs or WorkflowRunService(), keys, t_ids)
    return result
