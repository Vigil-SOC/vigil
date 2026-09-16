"""Is a threat report already covered by a hunt? (#903)

One answer in three shapes, composed from two existing reads and nothing else:
``parse_report`` turns the report into Entity Keys and T-IDs, ``list_prior_hunts``
says what has hunted them. In-flight wins over concluded (epic #886, decision 2):

- ``running``   -- a hunt is on it now; extend that run rather than start another.
- ``concluded`` -- a distilled Verdict already covers it; the rows say what was
  found, and a proposal is still offered should the operator want a fresh look.
- ``uncovered`` -- nobody has hunted it; the proposal is a body
  ``POST /api/workflows/threat-hunt/execute`` accepts as-is.

Nothing here starts a hunt or files a directive. Both the ``check_hunt_coverage``
backend tool and the ``/workflows/threat-hunt/coverage`` route call this.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.memory.prior_hunts import list_prior_hunts
from core.threat_intel.threat_feed_service import parse_report

EXECUTE_PATH = "/api/workflows/threat-hunt/execute"
EXTEND_PATH = "/api/agent-runs/{run_id}/directives"


def _matched(rows: Sequence[Dict[str, Any]], field: str) -> set:
    return {value for row in rows for value in row.get(field) or ()}


def _split(asked: Sequence[str], hit: set) -> Dict[str, List[str]]:
    return {
        "matched": [value for value in asked if value in hit],
        "unmatched": [value for value in asked if value not in hit],
    }


def build_proposal(keys: Sequence[str], techniques: Sequence[str]) -> Dict[str, Any]:
    """A ``WorkflowExecuteRequest`` body. The T-IDs go in the hypothesis text
    because that is the only place the request carries them, and it is where
    the in-flight matcher reads them from once the hunt is running."""
    parts = []
    if keys:
        parts.append(f"indicators {', '.join(keys)}")
    if techniques:
        parts.append(f"techniques {', '.join(techniques)}")
    hypothesis = (
        f"Activity from the reported {' and '.join(parts)} is present in the "
        "environment"
    )
    return {
        "hypothesis": hypothesis,
        "hypothesis_subjects": {hypothesis: list(keys)},
        "approve_hypotheses": True,
    }


def check_coverage(
    report: Optional[str] = None,
    entity_keys: Iterable[str] = (),
    techniques: Iterable[str] = (),
) -> Dict[str, Any]:
    """Classify a report (and/or already-parsed keys and T-IDs) as
    ``running``, ``concluded`` or ``uncovered``.

    Raises ``ValueError`` when there is nothing to ask about: an empty report
    with no keys is a caller mistake, not a coverage answer.
    """
    parsed = parse_report(report) if report else {"entity_keys": [], "techniques": []}
    prior = list_prior_hunts(
        [*parsed["entity_keys"], *entity_keys],
        [*parsed["techniques"], *techniques],
    )
    keys, t_ids = prior["keys"], prior["techniques"]
    if not keys and not t_ids:
        raise ValueError("nothing to check: no entity keys or techniques were found")

    if prior["in_flight"]:
        status, rows = "running", prior["in_flight"]
    elif prior["concluded"]:
        status, rows = "concluded", prior["concluded"]
    else:
        status, rows = "uncovered", []

    key_split = _split(keys, _matched(rows, "matched_keys"))
    technique_split = _split(t_ids, _matched(rows, "matched_techniques"))
    result: Dict[str, Any] = {
        "status": status,
        "keys": keys,
        "techniques": t_ids,
        "matched_keys": key_split["matched"],
        "unmatched_keys": key_split["unmatched"],
        "matched_techniques": technique_split["matched"],
        "unmatched_techniques": technique_split["unmatched"],
    }
    if status == "running":
        result["in_flight"] = rows
        result["extend"] = {
            "method": "POST",
            "path": EXTEND_PATH,
            "kind": "extend",
            "run_ids": [row["run_id"] for row in rows],
        }
    else:
        if status == "concluded":
            result["concluded"] = rows
        result["proposal"] = build_proposal(keys, t_ids)
        result["execute"] = {"method": "POST", "path": EXECUTE_PATH}
    return result
