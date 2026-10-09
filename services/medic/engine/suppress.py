"""Static dependency suppression and routing times (semantics.md §6, §7).

The engine decides *when* an incident routes, because that depends on engine state:
which parents are open, whether the child's condition is still true 10 min after the
last parent resolved, and whether an upgrade window is open. The router (G3) gives
the incident its lane and what each autonomy level would have done.

Suppression entries are the pack's `suppression.yaml` content: `parent` and
`children` selectors ({rule}, {mode}, {cause} or {class}) and an optional `match`
list of group labels the two must agree on. Cycles are the pack loader's (F6).
"""

from __future__ import annotations

from dataclasses import dataclass

from services.medic.engine.history import gkey

HOLD_S = 300  # a child waits 5 min after firing before it routes (§6.2)
OUTLIVE_S = 600  # …and routes on its own if still firing and true 10 min later (§6.3)
SELECTORS = ("rule", "mode", "cause", "class")


@dataclass
class Open:
    """One open incident (firing or resolving) after this tick's state machine step."""

    rule_id: str
    fault: dict | None  # None for the built-in sensor-blind rule
    group: dict
    m: dict  # its machine: the routing fields live here and are persisted
    value: bool | None  # this tick's evaluation


def check(entries) -> list[dict]:
    """Suppression entries, shape-checked. A malformed one is refused, not skipped."""
    out = []
    for e in entries:
        ok = isinstance(e, dict) and set(e) <= {"parent", "children", "match"}
        ok = ok and all(_selector(e.get(k)) for k in ("parent", "children"))
        match = e.get("match", []) if ok else None
        if not ok or not (
            isinstance(match, list) and all(isinstance(x, str) for x in match)
        ):
            raise ValueError(f"bad suppression entry: {e!r}")
        out.append(e)
    return out


def _selector(sel) -> bool:
    return (
        isinstance(sel, dict)
        and len(sel) == 1
        and next(iter(sel)) in SELECTORS
        and isinstance(next(iter(sel.values())), str)
    )


def selects(sel: dict, rule_id: str, fault: dict | None) -> bool:
    ((what, value),) = sel.items()
    if what == "rule":
        return rule_id == value
    if fault is None:
        return False
    return value in fault["causes"] if what == "cause" else fault[what] == value


def route(
    now: float, opens: list[Open], entries: list[dict], upgrading: bool, blind: str
) -> list[tuple[dict, dict]]:
    """Advance every open incident's routing; returns (machine, change fields) for
    each `incident_updated` record to write. An incident that routes on the tick it
    opened is marked in its machine (`routed_at` = now), not given a second record."""
    out: list[tuple[dict, dict]] = []
    for inc in opens:
        m = inc.m
        if m.get("routed_at") is not None:
            continue
        mine = [
            e
            for e in entries
            if inc.rule_id != blind and selects(e["children"], inc.rule_id, inc.fault)
        ]
        parents = [
            p
            for e in mine
            for p in opens
            if p is not inc
            and selects(e["parent"], p.rule_id, p.fault)
            and all(p.group.get(n) == inc.group.get(n) for n in e.get("match", []))
        ]
        if parents:  # §6.1: suppressed while any parent is open
            by = min(
                parents, key=lambda p: (p.m["opened_at"], p.rule_id, gkey(p.group))
            )
            m["freed_at"] = None
            if not m.get("suppressed_by"):
                m["suppressed_by"] = by.m["incident_id"]
                out.append(
                    (m, {"change": "suppressed", "suppressed_by": by.m["incident_id"]})
                )
            continue
        if m.get("suppressed_by"):  # §6.3: the last parent has resolved
            m["freed_at"] = m.get("freed_at") or now
            if now - m["freed_at"] < OUTLIVE_S or upgrading:
                continue
            if m["state"] == "firing" and inc.value is True:
                m["suppressed_by"], m["routed_at"] = None, now
                out += [(m, {"change": "unsuppressed"}), (m, {"change": "routed"})]
            # Resolving or unknown: wait. One that resolves closes without ever
            # routing (it was only a symptom); one that refires true routes then.
            continue
        if (mine and now < m["hold_end"]) or upgrading:  # §6.2 hold, §7
            continue
        m["routed_at"] = now
        if m["opened_at"] != now:
            out.append((m, {"change": "routed"}))
    return out
