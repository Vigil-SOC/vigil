"""The engine: observations and a tick time in, engine records out (semantics.md §4).

A pure library. No I/O, no wall clock, no randomness: the instance_id is passed in,
and every record's `at` is the tick. Each rule × group runs the state machine
inactive → pending → firing → resolving, with flapping damping (§5), upgrade
windows (§7), dependency suppression and routing times (§6), a group cap with one
overflow group, and group retirement (§3). The engine writes the fields of
incident_opened / incident_updated / incident_resolved that it owns, including
`route`; the router (G3) adds lane, runbook and would_have, and the store (S2)
seals the chain.
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
from services.medic.engine import suppress
from services.medic.engine.history import History, gkey, sample_entries
from services.medic.engine.loader import DEFAULT_FOR_S, DEFAULT_KEEP_S, LoadedRule
from services.medic.engine.upgrade import UpgradeWatch

SHAPES = ("start_sh", "compose", "helm")
TICK_S = 15
BLIND = "watcher.sensor-blind"  # built-in (§2, K1 T-09): one group per sensor
GROUP_CAP = 64  # live groups per rule; the rest share one overflow group (§3)
OVERFLOW = "__overflow__"
RETIRE_S = 24 * 3600  # covered silence before a group retires (§3)
FLAP_OPENS, FLAP_WINDOW_S, FLAP_HOLD_S = 3, 3600, 1800  # §5
OPEN = ("firing", "resolving")
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
        suppression: list[dict] = (),  # the pack's suppression.yaml entries (§6)
        group_cap: int = GROUP_CAP,
    ) -> None:
        if not _INSTANCE.match(instance_id) or install_shape not in SHAPES:
            raise ValueError(
                "instance_id must be mi_ + 16 hex; shape one of " + str(SHAPES)
            )
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("duplicate rule ids")
        if not isinstance(group_cap, int) or group_cap < 1:
            raise ValueError("group_cap must be a positive integer")
        self.instance_id, self.shape = instance_id, install_shape
        self.suppression, self.group_cap = suppress.check(suppression), group_cap
        self.rules = sorted(rules, key=lambda r: r.id)
        self.by_id = {r.id: r for r in self.rules}
        self.active = [
            r
            for r in self.rules
            if r.skipped is None and install_shape in r.rule.get("shapes", SHAPES)
        ]
        state = copy.deepcopy(state) if state else {"v": 1, "machines": {}}
        if state.get("v") != 1:
            raise ValueError("unknown engine state version")
        self.machines: dict[str, dict[str, dict]] = state["machines"]
        for m in (m for slots in self.machines.values() for m in slots.values()):
            if m.get("incident_id") and "opened_at" not in m:  # part-1 state:
                since = m["active_since"]  # it routed when it opened (no §6 then)
                m |= {"opened_at": since, "hold_end": since, "routed_at": since}
        self.last_tick: float | None = state.get("last_tick")
        self.history = History(state.get("latches", {}))
        # sensor -> [start of its current covered run, newest ok read, interval_s]
        self.cover: dict[str, list[float]] = state.get("cover", {})
        self.upgrade = UpgradeWatch(state.get("upgrade"))
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
            | {"last_tick": self.last_tick, "cover": self.cover}
            | {"upgrade": self.upgrade.state()}
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
        found: dict[str, dict] = {}  # rule -> new group key -> [first t, last t]
        for obs in sorted(ready, key=lambda o: (ev.ts(o["t"]), o["id"])):
            self._ingest(obs, found)
        self._place(found, now)
        records, rows, opens, retired = self._retire_removed_rules(now), [], [], []
        upgrading = self.upgrade.active(now)
        for rule_id in sorted({r.id for r in self.active} | {BLIND}):
            rule = self.by_id.get(rule_id) if rule_id != BLIND else None
            for key, m in sorted(self.machines.get(rule_id, {}).items()):
                group = dict(json.loads(key))
                value, evidence, untrusted = self._evaluate(rule, key, m, now)
                records += self._step(rule, group, m, value, evidence, now, upgrading)
                gone = self._retire(rule, key, m, now)
                if gone is not None:
                    records += gone
                    retired.append((rule_id, key))
                elif m["state"] in OPEN:
                    fault = rule.rule["fault"] if rule else None
                    opens.append(suppress.Open(rule_id, fault, group, m, value))
                rows.append((rule_id, group, m, value, untrusted))
        for m, fields in suppress.route(now, opens, self.suppression, upgrading, BLIND):
            records.append(_record("incident_updated", now, m, **fields))
        routed_now = {o.m["incident_id"] for o in opens if o.m.get("routed_at") == now}
        for rec in records:  # an incident routes on its opening tick, or is held
            if rec["type"] == "incident_opened":
                held = rec["body"]["incident_id"] not in routed_now
                rec["body"]["route"] = "held" if held else "routed"
        status = [
            {"rule": rule_id, "group": group, "eval": EVAL[value]}
            | {"state": m["state"], "incident_id": m.get("incident_id")}
            | {"suppressed": bool(m.get("suppressed_by"))}
            | {"flapping": bool(m.get("flapping")), "runtime_untrusted": untrusted}
            for rule_id, group, m, value, untrusted in rows
        ]
        for rule_id, key in retired:  # its state is dropped (§3)
            del self.machines[rule_id][key]
        for r in self.rules:  # never shown healthy: H2 "can't evaluate"
            why = None
            if r not in self.active:
                why = "needs_engine_minor" if r.skipped else "shape"
            elif not self.machines.get(r.id):
                why = "no_groups"
            if why:
                status.append(
                    {"rule": r.id, "group": {}, "eval": "unknown", "state": "inactive"}
                    | {"incident_id": None, "suppressed": False, "flapping": False}
                    | {"runtime_untrusted": False}
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

    # Ingest: index the observation, track coverage and upgrades, discover groups,
    # advance latches.

    def _ingest(self, obs: dict, found: dict[str, dict[str, float]]) -> None:
        self.history.add(obs)
        t = ev.ts(obs["t"])
        self._cover(obs, t)
        self.upgrade.observe(obs, t)
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
                    self._saw(r, gkey(group), t, found)
                if kind == "log" and ev.line_matches(spec, obs):
                    self._latch(r, name, spec, obs, t)

    def _cover(self, obs: dict, t: float) -> None:
        """Each sensor's current covered run (§2): ok reads or ok heartbeats never
        more than 2 × interval apart. Kept as state: it outlives the raw history."""
        if obs["kind"] == "sensor_health":
            if obs["health"]["state"] != "ok":
                return
            sensor, interval = obs["health"]["sensor"], obs["health"]["interval_s"]
        else:
            interval = self.history.interval.get(obs["signal"])
            if obs["outcome"] != "ok" or interval is None:
                return
            sensor = obs["sensor"]["id"]
        run = self.cover.get(sensor)
        if run is not None and t <= run[1]:
            return  # late: it can't extend the run, and a gap is already decided
        if run is None or t - run[1] > 2 * interval:
            self.cover[sensor] = [t, t, interval]
        else:
            run[1], run[2] = t, interval

    def _saw(self, r: LoadedRule, key: str, t: float, found: dict) -> None:
        """An observation of a group (§3): note when, or queue a new group."""
        slots = self.machines.setdefault(r.id, {})
        over = slots.get(_overflow_key(r), {}).get("members", {})
        if key in slots:
            slots[key]["seen"] = max(slots[key].get("seen", t), t)
        elif key in over:
            over[key] = max(over[key], t)
        else:
            first, last = found.setdefault(r.id, {}).setdefault(key, [t, t])
            found[r.id][key] = [min(first, t), max(last, t)]

    def _place(self, found: dict[str, dict[str, list[float]]], now: float) -> None:
        """New groups get their own machine in order of first appearance (ties by
        label values) until the rule has `group_cap` live groups; the rest join the
        overflow group, which is true if any of them is true (§3). A group whose
        newest observation is already 24 h old isn't live: that is re-fed history of
        a group that retired before a restart, and it stays retired."""
        for rule_id, new in sorted(found.items()):
            slots, okey = self.machines[rule_id], _overflow_key(self.by_id[rule_id])
            for key in sorted(new, key=lambda k: (new[k][0], k)):
                last = new[key][1]
                if now - last >= RETIRE_S:
                    continue
                if len(slots) - (okey in slots) < self.group_cap:
                    slots[key] = {"state": "inactive", "seen": last}
                else:
                    over = slots.setdefault(okey, {"state": "inactive", "members": {}})
                    over["members"][key] = last

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

    def _evaluate(self, rule: LoadedRule | None, key: str, m: dict, now: float):
        if "members" in m:  # the overflow group: true if any member is true (§3)
            values, evidence, untrusted = [], {}, False
            for member in sorted(m["members"]):
                value, got, tainted = self._evaluate(rule, member, {}, now)
                values.append(value)
                untrusted |= tainted
                for name, read in got.items() if value is True else ():
                    if name not in evidence or read[0] >= evidence[name][0]:
                        evidence[name] = read
            return ev.k_any(values), evidence, untrusted
        group = dict(json.loads(key))
        if rule is None:  # built-in sensor-blind: the sensor's newest heartbeat (§2)
            beats = self.history.beats.get(group["sensor"], [])
            beats = [b for b in beats if b[0] <= now]
            if not beats:  # e.g. restored state, raw history gone: can't tell
                return None, {}, False
            blind = beats[-1][1]["health"]["state"] in ("blind", "stopped")
            return blind, {"sensor_health": (*beats[-1], None)}, False
        ctx = ev.Context(rule, group, now, self.history)
        return ev.evaluate(rule.rule["when"], ctx), ctx.evidence, ctx.untrusted

    # Retirement (§3): 24 h of covered silence ends a group.

    def _retire(self, rule, key: str, m: dict, now: float) -> list[dict] | None:
        """None if the group stays; else the records of its retirement."""
        if rule is None or not rule.rule.get("group_by"):
            return None  # sensor-blind groups and the {} group never retire
        if "members" in m:  # the overflow group never retires; its members do
            for member, seen in sorted(m["members"].items()):
                if self._silent(rule, member, seen, now):
                    del m["members"][member]
                    self._drop_latches(rule, member)
            return None
        if not self._silent(rule, key, m.get("seen"), now):
            return None
        out = []
        if m.get("incident_id") and m["state"] in OPEN:
            out.append(_record("incident_resolved", now, m, how="group_retired"))
        self._drop_latches(rule, key)
        m.clear()
        m["state"] = "inactive"
        return out

    def _silent(self, rule: LoadedRule, key: str, seen: float | None, now: float):
        if seen is None or now - seen < RETIRE_S:
            return False
        group = dict(json.loads(key))
        for node in _latches(rule.rule["when"]):  # a set latch never retires
            set_t, clear_t = self.history.latch(rule.id, node, group)
            if set_t is not None and (clear_t is None or set_t > clear_t):
                return False
        sensors = {
            self.history.sensor_of.get(next(iter(sig.values()))["signal"])
            for sig in rule.rule["signals"].values()
        }
        return all(self._covered_day(s, now) for s in sensors)

    def _covered_day(self, sensor: str | None, now: float) -> bool:
        """Covered for all of [now − 24 h, now]; a blind period restarts the run."""
        run = self.cover.get(sensor) if sensor else None
        return (
            run is not None and run[0] <= now - RETIRE_S and now - run[1] <= 2 * run[2]
        )

    def _drop_latches(self, rule: LoadedRule, key: str) -> None:
        for slots in self.history.latches.get(rule.id, {}).values():
            slots.pop(key, None)

    def _step(self, rule, group, m, value, evidence, now, upgrading) -> list[dict]:
        hold = rule.seconds("for", DEFAULT_FOR_S) if rule else 120
        keep = rule.seconds("keep_firing_for", DEFAULT_KEEP_S) if rule else 0
        keep = FLAP_HOLD_S if m.get("flapping") else keep  # §5 replaces it
        st, out, opened = m["state"], [], False
        if st == "inactive" and value is True:
            st, m["active_since"] = "pending", now
        elif st == "pending" and value is False:
            st, m["active_since"] = "inactive", None
            m.pop("held_until", None)
        if st == "pending" and value is True and now - m["active_since"] >= hold:
            if upgrading:  # §7: delayed, never hidden; active_since is kept
                m["held_until"] = self.upgrade.end
            else:
                st, opened = "firing", True
                out.append(self._open(rule, group, m, evidence, now))
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
            # §6.3: a suppressed child that never routed was only a symptom.
            quiet = m.get("suppressed_by") and m.get("routed_at") is None
            how = "closed_quietly" if quiet else "cleared"
            out.append(_record("incident_resolved", now, m, how=how, eval="false"))
            last = {k: m.get(k) for k in _INCIDENT}  # a flapping reopen needs it
            kept = {k: m[k] for k in ("seen", "opens", "members") if k in m}
            m.clear()
            m.update(kept, last=last)
            st = "inactive"
        elif st in OPEN and not opened:
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

    def _open(self, rule, group, m, evidence, now) -> dict:
        """A new incident, or (§5) the third opening within 60 min reopens the most
        recent one, marked flapping."""
        recent = [t for t in m.get("opens", []) if now - t <= FLAP_WINDOW_S]
        m["opens"] = recent[-(FLAP_OPENS - 1) :] + [now]
        held, last = m.pop("held_until", None), m.pop("last", None)
        m["hold_end"] = now + suppress.HOLD_S
        if len(recent) < FLAP_OPENS - 1 or not last:
            return self._opened(rule, group, m, evidence, now, held)
        m |= {k: last[k] for k in _INCIDENT} | {"flapping": True}
        m["reopen_count"] = (last["reopen_count"] or 0) + 1
        return _record(
            "incident_updated",
            now,
            m,
            change="reopened",
            reopen_count=m["reopen_count"],
        )

    def _opened(self, rule, group, m, evidence, now, held) -> dict:
        glist = [{"name": k, "value": v} for k, v in sorted(group.items())]
        rid = rule.id if rule else BLIND
        m["incident_id"] = decision_chain.incident_id(
            self.instance_id, rid, glist, iso(m["active_since"])
        )
        m["evidenced"] = sorted(evidence)
        m["opened_at"] = now
        return {
            "type": "incident_opened",
            "at": iso(now),
            "body": {
                "incident_id": m["incident_id"],
                "instance_id": self.instance_id,
                "install_shape": self.shape,
                "active_since": iso(m["active_since"]),
                "group": glist,
                "route": "held",  # the routing pass (§6) sets it at the end of the tick
                **({"held_by_upgrade_until": iso(held)} if held else {}),
                "evidence": _items(evidence),
                **_meta(rule),
            },
        }


# What a flapping reopen restores of the most recent incident (§5).
_INCIDENT = (
    "incident_id",
    "evidenced",
    "opened_at",
    "routed_at",
    "suppressed_by",
    "reopen_count",
)


def _overflow_key(rule: LoadedRule) -> str:
    return gkey({n: OVERFLOW for n in rule.rule.get("group_by", [])})


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
