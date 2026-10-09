"""G1 reference: how the router gives an incident its lane, when it routes, and what
each autonomy level would have done.

Executable form of contracts/decision-record.md section 2. Incidents are E3's
(one per rule x group, semantics.md section 4); suppression and the routing hold are
E3 section 6. The router (G3) must produce the same outputs for every
contracts/vectors/lanes/lanes-*.yaml vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ESCALATE_AFTER_FAILURES = 2  # Phase 1 1C5: two failed attempts escalate to lane 3
ROUTING_HOLD_S = 300  # E3 6.2
OUTLIVE_S = 600  # E3 6.3


@dataclass(frozen=True)
class Incident:
    id: str
    rule: str | None  # None = unknown signature (no rule claims it)
    lane: int | None
    opened_at: int
    resolved_at: int | None = None  # None = still open at the vector's end
    trust: str = "trusted"
    signals: int = 1
    samples: int = 0
    runtime_untrusted: bool = False
    runbook: dict[str, Any] | None = None
    # E3 4: spans spent resolving, as (from, refired_at); refired_at None = it never
    # refired (it resolved at resolved_at, or is still resolving at the end).
    resolving: tuple[tuple[int, int | None], ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Incident:
        spans = tuple(tuple(s) for s in d.get("resolving", ()))
        return cls(**(d | {"resolving": spans}))

    def firing_from(self, t: int) -> int | None:
        """The first time >= t at which the incident is firing, or None if it never
        is again (resolving until it resolves, or until the vector ends)."""
        if self.resolved_at is not None and self.resolved_at <= t:
            return None
        for start, refired in self.resolving:
            if start <= t and (refired is None or refired > t):
                return refired
        return t


def lane_of(inc: Incident) -> dict[str, Any]:
    """R1, R3, R4: the lane comes from rule metadata only, never from evidence text."""
    if inc.rule is None:
        return {"value": 3, "reason": "unknown_signature"}
    if inc.lane == 1 and (inc.trust != "trusted" or inc.signals < 2 or inc.samples < 1):
        # A pack that passed E-LANE1 can't do this, so treat it as a defect.
        return {"value": 3, "reason": "lane1_gate_failed"}
    assert inc.lane in (1, 2, 3)
    return {"value": inc.lane, "reason": "rule"}


def would_have(inc: Incident, lane: int) -> dict[str, Any]:
    """R8: what each autonomy level would have done."""
    if lane == 2:
        return {
            "L0": "record",
            "L1": "notify_admin",
            "L2": {"action": "notify_admin", "not_eligible": "not_lane1"},
        }
    if lane == 3:
        return {
            "L0": "record",
            "L1": "prepare_support_bundle",
            "L2": {"action": "prepare_support_bundle", "not_eligible": "not_lane1"},
        }
    if inc.runbook is None:
        return {
            "L0": "record",
            "L1": "notify_admin",
            "L2": {"action": "notify_admin", "not_eligible": "no_runbook"},
        }
    if inc.runtime_untrusted:
        return {
            "L0": "record",
            "L1": "propose_runbook",
            "L2": {"action": "propose_runbook", "not_eligible": "runtime_untrusted"},
        }
    return {"L0": "record", "L1": "propose_runbook", "L2": {"action": "run_runbook"}}


def route(
    inc: Incident,
    incidents: list[Incident],
    parents_of: dict[str, set[str]],
    until: int,
) -> tuple[int | None, str | None]:
    """R2 (E3 6): returns (routed_at, suppressed_by).

    A child holds 5 min. A parent open at any time during the hold suppresses it. It
    routes on its own only if it is still firing 10 min after the last parent
    resolves; one resolving then routes if it refires, and never if it resolves (E3
    6.3 as amended 2026-10-08, S4b2-4/5). "Firing" stands in for E3's "firing and
    true": an unknown tick while firing is the engine vectors' to pin (v19).
    """
    parents = parents_of.get(inc.rule or "", set())
    if not parents:
        return inc.opened_at, None
    hold_end = inc.opened_at + ROUTING_HOLD_S
    blockers = [
        p
        for p in incidents
        if p.rule in parents
        and p.opened_at <= hold_end
        and (p.resolved_at is None or p.resolved_at > inc.opened_at)
    ]
    if not blockers:
        return hold_end, None
    by = min(blockers, key=lambda p: p.opened_at).id
    ends = [p.resolved_at for p in blockers]
    if None in ends:
        return None, by
    free_at = max(e for e in ends if e is not None) + OUTLIVE_S
    at = inc.firing_from(free_at)
    return (at if at is not None and at <= until else None), by


def run_vector(vector: dict[str, Any]) -> dict[str, Any]:
    incidents = [Incident.from_dict(d) for d in vector["incidents"]]
    parents_of: dict[str, set[str]] = {}
    for pair in vector.get("suppression", []):
        parents_of.setdefault(pair["child"], set()).add(pair["parent"])
    until = vector["until"]
    out: dict[str, Any] = {}
    for inc in incidents:
        lane = lane_of(inc)
        routed_at, suppressed_by = route(inc, incidents, parents_of, until)
        wh = would_have(inc, lane["value"])
        escalation = None
        # R6: only a fix L2 would have run can "fail".
        if routed_at is not None and wh["L2"]["action"] == "run_runbook":
            assert inc.runbook is not None
            at = routed_at + ESCALATE_AFTER_FAILURES * inc.runbook["verify_after_s"]
            end = until if inc.resolved_at is None else inc.resolved_at
            if end >= at:
                escalation = {"at": at, "simulated_failures": ESCALATE_AFTER_FAILURES}
        out[inc.id] = {
            "lane": lane,
            "routed_at": routed_at,
            "suppressed_by": suppressed_by,
            "would_have": wh,
            "escalation": escalation,
        }
    return out
