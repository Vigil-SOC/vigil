"""The engine: observations and a tick time in, engine records out (semantics.md §4).

A pure library. No I/O, no wall clock, no randomness: the instance_id is passed in,
and every record's `at` is the tick. Each rule × group runs the state machine
inactive → pending → firing → resolving. The engine writes the fields of
incident_opened / incident_updated / incident_resolved that it owns; the router
(G3) adds lane, route, runbook and would_have, and the store (S2) seals the chain.
State that must survive a restart is `state()`; the caller persists it (C4) and
passes it back, then re-feeds whatever raw history it still holds.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime

from services.medic.contracts import decision_chain
from services.medic.contracts.rule_check import seconds
from services.medic.engine import evaluate as ev
from services.medic.engine.history import History, gkey, sample_entries
from services.medic.engine.loader import DEFAULT_FOR_S, DEFAULT_KEEP_S, LoadedRule

SHAPES = ("start_sh", "compose", "helm")
TICK_S = 15
BLIND = "watcher.sensor-blind"  # built-in (§2, K1 T-09): one group per sensor
_INSTANCE = re.compile(r"^mi_[0-9a-f]{16}$")
_DECIMAL = re.compile(r"^-?[0-9]{1,15}(\.[0-9]{1,6})?$")
EVAL = {True: "true", False: "false", None: "unknown"}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Engine:
    def __init__(
        self,
        rules: list[LoadedRule],
        *,
        instance_id: str,
        install_shape: str,
        state: dict | None = None,
    ) -> None:
        if not _INSTANCE.match(instance_id) or install_shape not in SHAPES:
            raise ValueError(
                "instance_id must be mi_ + 16 hex; shape one of " + str(SHAPES)
            )
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("duplicate rule ids")
        self.instance_id, self.shape = instance_id, install_shape
        self.rules = sorted(rules, key=lambda r: r.id)
        self.active = [
            r
            for r in self.rules
            if r.skipped is None and install_shape in r.rule.get("shapes", SHAPES)
        ]
        state = copy.deepcopy(state) if state else {"v": 1, "machines": {}}
        if state.get("v") != 1:
            raise ValueError("unknown engine state version")
        self.machines: dict[str, dict[str, dict]] = state["machines"]
        self.last_tick: float | None = state.get("last_tick")
        self.history = History(state.get("latches", {}))
        self.pending: list[dict] = []
        self._status: list[dict] = []
        self._horizon = max(
            (_max_window(r.rule["when"], r) for r in self.active), default=0
        )
        for r in self.active:
            if not r.rule.get("group_by"):
                self._machine(r.id, {})

    def state(self) -> dict:
        return copy.deepcopy(
            {"v": 1, "machines": self.machines, "latches": self.history.latches}
            | {"last_tick": self.last_tick}
        )

    def observe(self, observation: dict) -> None:
        """Accept one D3 observation. It counts from the first tick ≥ its observed_at."""
        self.pending.append(observation)

    def status(self) -> list[dict]:
        """Every rule × group after the last tick: eval, state and the open incident."""
        return copy.deepcopy(self._status)

    def tick(self, at: datetime) -> list[dict]:
        if at.tzinfo is None:
            raise ValueError("tick time must be timezone-aware UTC")
        now = at.timestamp()
        if now % TICK_S or (self.last_tick is not None and now <= self.last_tick):
            raise ValueError(
                f"tick {at} is off the {TICK_S} s grid or not after the last"
            )
        self.last_tick = now
        ready = [o for o in self.pending if ev.ts(o["observed_at"]) <= now]
        self.pending = [o for o in self.pending if ev.ts(o["observed_at"]) > now]
        for obs in ready:
            self._ingest(obs)
        records, status = self._retire_removed_rules(now), []
        for rule_id in sorted({r.id for r in self.active} | {BLIND}):
            rule = next((r for r in self.active if r.id == rule_id), None)
            for key, m in sorted(self.machines.get(rule_id, {}).items()):
                group = dict(json.loads(key))
                value, evidence, untrusted = self._evaluate(rule, group, now)
                records += self._step(rule, group, m, value, evidence, now)
                status.append(
                    {"rule": rule_id, "group": group, "eval": EVAL[value]}
                    | {"state": m["state"], "incident_id": m.get("incident_id")}
                    | {"runtime_untrusted": untrusted}
                )
        for r in self.rules:  # never shown healthy: H2 "can't evaluate"
            why = None
            if r not in self.active:
                why = "needs_engine_minor" if r.skipped else "shape"
            elif not self.machines.get(r.id):
                why = "no_groups"
            if why:
                status.append(
                    {"rule": r.id, "group": {}, "eval": "unknown", "state": "inactive"}
                    | {"incident_id": None, "runtime_untrusted": False}
                    | {"not_evaluated": why}
                )
        self._status = status
        reach = self._horizon + 2 * max(self.history.interval.values(), default=0)
        self.history.prune(now - reach - TICK_S)
        return records

    def _retire_removed_rules(self, now: float) -> list[dict]:
        """A rule gone from the rule set ends its open incidents at once (§4)."""
        known = {r.id for r in self.rules} | {BLIND}
        out = []
        for rule_id in sorted(set(self.machines) - known):
            for _, m in sorted(self.machines.pop(rule_id).items()):
                if m.get("incident_id"):
                    out.append(_record("incident_resolved", now, m, how="rule_retired"))
            self.history.latches.pop(rule_id, None)
        return out

    # Ingest: index the observation, discover groups, advance latches.

    def _ingest(self, obs: dict) -> None:
        self.history.add(obs)
        t = ev.ts(obs["t"])
        if obs["kind"] == "sensor_health":
            self._machine(BLIND, {"sensor": obs["health"]["sensor"]})
            return
        for r in self.active:
            group_by = r.rule.get("group_by", [])
            for name, sig in r.rule["signals"].items():
                kind, spec = next(iter(sig.items()))
                if spec["signal"] != obs["signal"]:
                    continue
                for group in _groups(kind, spec, group_by, obs):
                    self._machine(r.id, group)
                if kind == "log" and ev.line_matches(spec, obs):
                    self._latch(r, name, spec, obs, t)

    def _latch(self, r: LoadedRule, name: str, spec: dict, obs: dict, t: float) -> None:
        """A matching set or clear line advances the latch of its group (or all, "*")."""
        group_by = r.rule.get("group_by", [])
        own = bool(group_by) and set(group_by) <= set(spec.get("keys", {}))
        got = ev.line_group(spec, obs) if own else {}
        if got is None:
            return
        key = gkey({n: got[n] for n in group_by}) if own else "*"
        for node in _latches(r.rule["when"]):
            for which, ref in enumerate((node["set"], node["clear"])):
                if ref == name:
                    self.history.note_latch(r.id, node, which, key, t)

    def _machine(self, rule_id: str, group: dict) -> dict:
        slots = self.machines.setdefault(rule_id, {})
        return slots.setdefault(gkey(group), {"state": "inactive"})

    # Evaluate and step.

    def _evaluate(self, rule: LoadedRule | None, group: dict, now: float):
        if rule is None:  # built-in sensor-blind: the sensor's newest heartbeat (§2)
            beats = self.history.beats.get(group["sensor"], [])
            beats = [b for b in beats if b[0] <= now]
            if not beats:  # e.g. restored state, raw history gone: can't tell
                return None, {}, False
            blind = beats[-1][1]["health"]["state"] in ("blind", "stopped")
            return blind, {"sensor_health": (*beats[-1], None)}, False
        ctx = ev.Context(rule, group, now, self.history)
        return ev.evaluate(rule.rule["when"], ctx), ctx.evidence, ctx.untrusted

    def _step(self, rule, group, m, value, evidence, now) -> list[dict]:
        hold = rule.seconds("for", DEFAULT_FOR_S) if rule else 120
        keep = rule.seconds("keep_firing_for", DEFAULT_KEEP_S) if rule else 0
        st, out, opened = m["state"], [], False
        if st == "inactive" and value is True:
            st, m["active_since"] = "pending", now
        elif st == "pending" and value is False:
            st, m["active_since"] = "inactive", None
        if st == "pending" and value is True and now - m["active_since"] >= hold:
            st, opened = "firing", True
            out.append(self._opened(rule, group, m, evidence, now))
        elif st == "firing" and value is False:
            st, m["resolving_since"] = "resolving", now
            out.append(
                _record("incident_updated", now, m, change="resolving", eval="false")
            )
        elif st == "resolving" and value is True:
            st, m["resolving_since"] = "firing", None
            out.append(
                _record("incident_updated", now, m, change="refiring", eval="true")
            )
        if st == "resolving" and value is False and now - m["resolving_since"] >= keep:
            out.append(
                _record("incident_resolved", now, m, how="cleared", eval="false")
            )
            m.clear()
            st = "inactive"
        elif st in ("firing", "resolving") and not opened:
            new = {k: r for k, r in evidence.items() if k not in m["evidenced"]}
            if new:
                m["evidenced"] = sorted({*m["evidenced"], *new})
                out.append(
                    _record(
                        "incident_updated",
                        now,
                        m,
                        change="evidence_added",
                        evidence=_items(new),
                    )
                )
        m["state"] = st
        return out

    def _opened(self, rule, group, m, evidence, now) -> dict:
        glist = [{"name": k, "value": v} for k, v in sorted(group.items())]
        rid = rule.id if rule else BLIND
        m["incident_id"] = decision_chain.incident_id(
            self.instance_id, rid, glist, iso(m["active_since"])
        )
        m["evidenced"] = sorted(evidence)
        return {
            "type": "incident_opened",
            "at": iso(now),
            "body": {
                "incident_id": m["incident_id"],
                "instance_id": self.instance_id,
                "install_shape": self.shape,
                "active_since": iso(m["active_since"]),
                "group": glist,
                "evidence": _items(evidence),
                **_meta(rule),
            },
        }


def _groups(kind: str, spec: dict, group_by: list, obs: dict) -> list[dict]:
    """Groups an observation reveals: sample `by` labels or log `keys` (§3)."""
    if not group_by:
        return []
    if kind == "sample" and set(group_by) <= set(spec.get("by", [])):
        if obs["outcome"] != "ok":
            return []
        found = [v.get("labels", {}) for v in sample_entries(spec, obs)]
        return [
            {n: lab[n] for n in group_by}
            for lab in found
            if all(n in lab for n in group_by)
        ]
    if kind == "log" and set(group_by) <= set(spec.get("keys", {})):
        got = ev.line_group(spec, obs) if ev.line_matches(spec, obs) else None
        return [{n: got[n] for n in group_by}] if got else []
    return []


def _children(node: dict) -> list[dict]:
    return (
        node.get("all", [])
        + node.get("any", [])
        + ([node["not"]] if "not" in node else [])
    )


def _latches(node: dict):
    if node.get("fn") == "latch":
        yield node
    for child in _children(node):
        yield from _latches(child)


def _max_window(node: dict, rule: LoadedRule) -> float:
    if "fn" not in node:
        return max(_max_window(k, rule) for k in _children(node))
    w = node.get("window")
    return rule.param(w["param"]) if isinstance(w, dict) else seconds(w) if w else 0


BLIND_META = {
    "subject": "watcher",
    "rule": {"id": BLIND, "revision": 1, "pack_id": "medic-builtin"}
    | {"pack_version": "1.0", "engine_api": "1.0", "input_trust": "trusted"},
    "fault": None,
    "detection_type": "event",
    "advice": None,
}


def _meta(rule: LoadedRule | None) -> dict:
    """The rule's half of incident_opened (decision-record.md §3)."""
    if rule is None:
        return copy.deepcopy(BLIND_META)
    r, (pack_id, pack_version) = rule.rule, rule.pack
    return {
        "subject": "vigil",
        "rule": {"id": r["id"], "revision": r["revision"], "pack_id": pack_id}
        | {"pack_version": pack_version, "engine_api": r.get("engine_api", "1.0")}
        | {"input_trust": rule.input_trust},
        "fault": {k: copy.deepcopy(r["fault"][k]) for k in ("class", "mode", "causes")},
        "detection_type": rule.detection,
        "advice": {k: r["advice"][k] for k in ("summary", "fix")},
    }


def _record(kind: str, now: float, m: dict, **fields) -> dict:
    return {
        "type": kind,
        "at": iso(now),
        "body": {"incident_id": m["incident_id"], **fields},
    }


def _items(evidence: dict) -> list[dict]:
    """G2 evidence: per signal its newest contributing observation, by (t, id), ≤ 10."""
    items = []
    for t, obs, entry in evidence.values():
        item = {"observation_id": obs["id"], "signal": obs["signal"], "t": obs["t"]}
        if obs["kind"] == "log":
            item["fingerprint"] = obs["log"]["fingerprint"]
            text = obs["log"]["message"][:500]
            if text:
                item["excerpt"] = {
                    "text": text,
                    "redaction_version": obs["redaction"]["version"],
                }
        elif obs["kind"] == "sensor_health":
            item |= {"key": "state", "value": obs["health"]["state"]}
        elif entry is not None:
            item["key"] = entry["key"]
            value = _scalar(entry)
            if value is not None:
                item["value"] = value
        items.append((t, obs["id"], item))
    return [i for _, _, i in sorted(items, key=lambda x: x[:2])][-10:]


def _scalar(entry: dict):
    """An evidence value: integer, boolean, decimal string or label-safe id."""
    v = entry.get("value")
    if entry.get("state") != "present" or entry.get("type") == "text":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        if float(v).is_integer():
            return int(v)
        text = f"{v:.6f}".rstrip("0")
        return text if _DECIMAL.match(text) else None
    return v if isinstance(v, str) and ev._ID.match(v) else None
