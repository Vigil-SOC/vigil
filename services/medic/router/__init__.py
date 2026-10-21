"""Lane router, R1 only (contracts/decision-record.md §2).

An incident's lane is its rule's lane, read from rule metadata and never from
evidence (K1 T-05). The router adds the fields of incident_opened the engine
doesn't own: `route`, `lane`, `runbook` and `would_have` (S4b-6 (a)). Every
incident routes when it opens. Suppression and the routing hold (R2), the lane-1
gate at routing time (R3), unknown signatures (R4) and simulated escalation (R6)
are the full router's (G3).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from services.medic.engine import LoadedRule
from services.medic.engine.core import BLIND

WATCHER_LANE = 3  # E3 §2 / R7: watcher.sensor-blind goes to DeepTempo Support


def would_have(lane: int) -> dict[str, Any]:
    """R8 with no runbook: G4 ships none yet, so lane 1 also notifies the admin."""
    if lane == 3:
        action = "prepare_support_bundle"
    else:
        action = "notify_admin"
    reason = "no_runbook" if lane == 1 else "not_lane1"
    return {
        "L0": "record",
        "L1": action,
        "L2": {"action": action, "not_eligible": reason},
    }


class Router:
    def __init__(self, rules: Iterable[LoadedRule]) -> None:
        self._lanes = {r.id: r.rule["lane"] for r in rules} | {BLIND: WATCHER_LANE}

    def route(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return the record with the router's fields; never changes its argument."""
        if record["type"] != "incident_opened":
            return copy.deepcopy(record)
        rule_id = record["body"]["rule"]["id"]
        if rule_id not in self._lanes:
            raise ValueError(f"no rule {rule_id!r} in the router's rule set")
        lane = self._lanes[rule_id]
        body = copy.deepcopy(record["body"]) | {
            "route": "routed",
            "lane": {"value": lane, "reason": "rule"},
            "runbook": None,
            "would_have": would_have(lane),
        }
        return copy.deepcopy(record) | {"body": body}
