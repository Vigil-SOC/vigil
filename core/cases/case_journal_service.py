"""Edits to the lists a Case carries: activities, timeline, steps, techniques, findings.

These are not rows of their own. They are JSON lists on the Case record,
so adding one is a read, an append and a write rather than an insert, and the
ordering and timestamp conventions are the only thing that makes a list of them
readable later.

They live here because two callers perform them -- the `/api/v1` routes and
Vigil's MCP tools -- and each had written the read-append-write out for itself,
against a different service: the routes through ``DatabaseDataService``, which
answers in dictionaries, the tools through ``DatabaseService``, which answers in
ORM objects. Two spellings of one operation over one column is how the two
surfaces come to disagree about what a Case says.

Each function answers ``None`` when the Case does not exist, so a caller can say
so in its own idiom -- a 404 from a route, an error payload from a tool -- and
raises nothing a caller has to know about.

The data service is passed in rather than held here. Constructing one runs
``init_database`` and a health check, so it is a resource a process owns once;
a module global caching it would be ambient state that the caller cannot see,
substitute, or test around.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.storage.database_data_service import DatabaseDataService
from core.time import utcnow

logger = logging.getLogger(__name__)


def _now() -> str:
    return utcnow().isoformat() + "Z"


def _existing(
    service: DatabaseDataService, case_id: str, field: str
) -> Optional[List[Any]]:
    """The Case's current value for ``field``, or None when there is no Case.

    An absent field reads as an empty list: a Case that has never had an
    activity has no activities, which is not the same as not existing.
    """
    case = service.get_case(case_id)
    if not case:
        return None
    return case.get(field) or []


def append_activity(
    service: DatabaseDataService,
    case_id: str,
    activity_type: str,
    description: str,
    details: Optional[Dict] = None,
) -> Optional[Dict]:
    """Record that something happened on this Case. Returns the new entry."""
    activities = _existing(service, case_id, "activities")
    if activities is None:
        return None

    entry = {
        "timestamp": _now(),
        "activity_type": activity_type,
        "description": description,
        "details": details or {},
    }
    activities.append(entry)

    if not service.update_case(case_id, activities=activities):
        logger.error("Could not append an activity to case %s", case_id)
        return None
    return entry


def append_resolution_step(
    service: DatabaseDataService,
    case_id: str,
    description: str,
    action_taken: str,
    result: Optional[str] = None,
) -> Optional[Dict]:
    """Record a remediation step. Returns the new step."""
    steps = _existing(service, case_id, "resolution_steps")
    if steps is None:
        return None

    step = {
        "timestamp": _now(),
        "description": description,
        "action_taken": action_taken,
        "result": result,
    }
    steps.append(step)

    if not service.update_case(case_id, resolution_steps=steps):
        logger.error("Could not append a resolution step to case %s", case_id)
        return None
    return step


def append_timeline_entry(
    service: DatabaseDataService,
    case_id: str,
    event_description: str,
    event_time: Optional[str] = None,
    event_type: str = "investigation",
    details: Optional[Dict] = None,
) -> Optional[Dict]:
    """Record when something happened, not when it was written down.

    ``event_time`` is the event's own time and defaults to now. The list is
    kept in that order rather than insertion order, because a timeline read
    out of order is worse than no timeline.
    """
    timeline = _existing(service, case_id, "timeline")
    if timeline is None:
        return None

    entry = {
        "timestamp": event_time or _now(),
        "event_type": event_type,
        "description": event_description,
        "details": details or {},
    }
    timeline.append(entry)
    timeline.sort(key=lambda e: e["timestamp"])

    if not service.update_case(case_id, timeline=timeline):
        logger.error("Could not append a timeline entry to case %s", case_id)
        return None
    return entry


def merge_mitre_techniques(
    service: DatabaseDataService, case_id: str, technique_ids: List[str]
) -> Optional[Dict]:
    """Add technique ids the Case does not already carry.

    A union, not an append: naming a technique twice is not a second sighting
    of it. Returns which ids were new and the full set afterwards.
    """
    existing = _existing(service, case_id, "mitre_techniques")
    if existing is None:
        return None

    before = set(existing)
    added = sorted(set(technique_ids) - before)
    combined = sorted(before | set(technique_ids))

    if not service.update_case(case_id, mitre_techniques=combined):
        logger.error("Could not add techniques to case %s", case_id)
        return None
    return {"added": added, "all": combined}


def link_finding(
    service: DatabaseDataService, case_id: str, finding_id: str
) -> Optional[bool]:
    """Attach a Finding to a Case. True if it was attached, False if already on.

    None when the Case does not exist. Linking a Finding twice is not an
    error and does not attach it twice.
    """
    finding_ids = _existing(service, case_id, "finding_ids")
    if finding_ids is None:
        return None
    if finding_id in finding_ids:
        return False

    finding_ids.append(finding_id)
    if not service.update_case(case_id, finding_ids=finding_ids):
        logger.error("Could not link finding %s to case %s", finding_id, case_id)
        return None
    return True


def unlink_finding(
    service: DatabaseDataService, case_id: str, finding_id: str
) -> Optional[bool]:
    """Detach a Finding. True if it was detached, False if it was not attached."""
    finding_ids = _existing(service, case_id, "finding_ids")
    if finding_ids is None:
        return None
    if finding_id not in finding_ids:
        return False

    finding_ids.remove(finding_id)
    if not service.update_case(case_id, finding_ids=finding_ids):
        logger.error("Could not unlink finding %s from case %s", finding_id, case_id)
        return None
    return True
