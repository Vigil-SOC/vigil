"""Lane router (contracts/decision-record.md §2): R1, R3, R4, R6 and R8.

An incident's lane is its rule's lane, read from rule metadata and never from
evidence (K1 T-05). A lane-1 rule that fails the E-LANE1 gate here goes to lane 3
(R3); an incident no rule claims goes to lane 3 (R4). The router adds the fields of
incident_opened the engine doesn't own: `lane`, `runbook` and `would_have`, fixed
at open. **The engine alone writes `route`** and the routing updates (S4b2-2):
when an incident routes depends on engine state (open parents, the 10-min outlive
check, upgrade windows, E3 §6/§7). `escalation` (R6) is a "would have" fact
computed from what the record and the engine's routing say.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from services.medic.engine import LoadedRule
from services.medic.engine.core import BLIND

WATCHER_LANE = 3  # E3 §2 / R7: watcher.sensor-blind goes to DeepTempo Support
ESCALATE_AFTER_FAILURES = 2  # Phase 1 1C5: two failed attempts escalate to lane 3


def _conditions(node: dict) -> Iterator[dict]:
    if "fn" in node:
        yield node
        return
    for child in node.get("all", []) + node.get("any", []):
        yield from _conditions(child)
    if "not" in node:
        yield from _conditions(node["not"])


def passes_lane1_gate(rule: LoadedRule) -> bool:
    """E-LANE1 (rule_check): trusted input, >= 2 used signals, at least one a sample."""
    used = {
        c[k]
        for c in _conditions(rule.rule["when"])
        for k in ("signal", "set", "clear")
        if k in c
    }
    kinds = [
        next(iter(rule.rule["signals"][n])) for n in used if n in rule.rule["signals"]
    ]
    return rule.input_trust == "trusted" and len(used) >= 2 and "sample" in kinds


def lane_of(rule: LoadedRule) -> dict[str, Any]:
    """R1 and R3: the rule's lane, unless a lane-1 rule fails the gate."""
    if rule.rule["lane"] == 1 and not passes_lane1_gate(rule):
        # A pack that passed E-LANE1 can't do this: a defect or tampering.
        return {"value": 3, "reason": "lane1_gate_failed"}
    return {"value": rule.rule["lane"], "reason": "rule"}


def would_have(
    lane: int, runbook: dict[str, Any] | None = None, runtime_untrusted: bool = False
) -> dict[str, Any]:
    """R8: what each autonomy level would have done."""
    if lane != 1:
        action = "prepare_support_bundle" if lane == 3 else "notify_admin"
        return {
            "L0": "record",
            "L1": action,
            "L2": {"action": action, "not_eligible": "not_lane1"},
        }
    if runbook is None:
        l1, l2 = (
            "notify_admin",
            {"action": "notify_admin", "not_eligible": "no_runbook"},
        )
    elif runtime_untrusted:  # R3: L2 is capped at proposing
        l1 = "propose_runbook"
        l2 = {"action": "propose_runbook", "not_eligible": "runtime_untrusted"}
    else:
        l1, l2 = "propose_runbook", {"action": "run_runbook"}
    return {"L0": "record", "L1": l1, "L2": l2}


def escalation(
    would: dict[str, Any],
    routed_at: int | None,
    verify_after_s: int | None,
    resolved_at: int | None,
    until: int,
) -> dict[str, Any] | None:
    """R6: still open 2 x verify_after_s after routing, when L2 would have run a
    runbook (only that can "fail"). `until` bounds an incident still open."""
    if routed_at is None or would["L2"]["action"] != "run_runbook":
        return None
    assert verify_after_s is not None
    at = routed_at + ESCALATE_AFTER_FAILURES * verify_after_s
    end = until if resolved_at is None else resolved_at
    if end < at:
        return None
    return {"at": at, "simulated_failures": ESCALATE_AFTER_FAILURES}


class Router:
    def __init__(
        self,
        rules: Iterable[LoadedRule],
        runbooks: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self._lanes = {r.id: lane_of(r) for r in rules}
        self._lanes[BLIND] = {"value": WATCHER_LANE, "reason": "rule"}
        self._runbooks = dict(runbooks or {})  # rule id -> G4 descriptor; none ship

    def route(
        self, record: dict[str, Any], status: Sequence[Mapping[str, Any]] = ()
    ) -> dict[str, Any]:
        """Return the record with the router's fields; never changes its argument.
        `status` is the engine's status() after the tick that wrote the record,
        joined by incident_id for the runtime-untrusted flag."""
        if record["type"] != "incident_opened":
            return copy.deepcopy(record)
        body = record["body"]
        if body.get("route") not in ("routed", "held"):
            raise ValueError("incident_opened without the engine's route")
        if body["rule"] is None:  # R4: no catch-all in v1, so this is defensive
            lane, runbook, untrusted = (
                {"value": 3, "reason": "unknown_signature"},
                None,
                False,
            )
        else:
            rule_id = body["rule"]["id"]
            if rule_id not in self._lanes:
                raise ValueError(f"no rule {rule_id!r} in the router's rule set")
            lane = self._lanes[rule_id]
            runbook = (
                copy.deepcopy(self._runbooks.get(rule_id))
                if lane["value"] == 1
                else None
            )
            untrusted = any(
                s.get("runtime_untrusted")
                for s in status
                if s.get("incident_id") == body["incident_id"]
            )
        out = copy.deepcopy(body) | {
            "lane": dict(lane),
            "runbook": runbook,
            "would_have": would_have(lane["value"], runbook, untrusted),
        }
        return copy.deepcopy(record) | {"body": out}
