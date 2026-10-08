"""Drive the engine through an E3 vector (semantics.md §8) on a fake clock.

The runner expands the vector into D3 observations with the contract's own
expander, feeds them in observed_at order, ticks every 15 s from start to end, and
keeps every record the engine emits plus its status at each checkpoint tick. The
incident view (routed_at, suppressed_by, flapping…) is read back from the records
alone, so a vector passes only if the records say what it expects. An
engine restart (`engine_restarts`) builds a new engine from the JSON round-trip of
the old one's state and re-feeds the raw history the store would hold (C4).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.medic.contracts.vector_expand import expand, secs
from services.medic.engine import Engine, load_rule, load_rule_file

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
VECTORS = sorted((CONTRACTS / "vectors").glob("v[0-9][0-9]-*.yaml"))
RULES_DIR = CONTRACTS / "fixtures" / "rules" / "valid"
INSTANCE = "mi_0123456789abcdef"


def rules_of(vector: dict) -> list:
    out = []
    for entry in vector["rules"]:
        params = entry.get("params")
        if "ref" in entry:
            out.append(load_rule_file(RULES_DIR / entry["ref"], params=params))
        else:
            out.append(load_rule(entry["inline"], params=params))
    return out


def group_key(group: dict) -> tuple:
    return tuple(sorted(group.items()))


class Run:
    def __init__(self, records: list[dict], status: dict, start: datetime) -> None:
        self.records = records
        self.status = status  # (offset s, rule, group key) -> status entry
        self.start = start

    def offset(self, at: str) -> int:
        """'2026-10-07T10:20:00Z' -> 1200: seconds after the vector's start."""
        return int((datetime.fromisoformat(at) - self.start).total_seconds())

    def incidents(self) -> dict[str, dict]:
        """incident_id -> the vector's incident shape (offsets in seconds), from the records."""
        out: dict[str, dict] = {}
        for rec in self.records:
            body, at = rec["body"], self.offset(rec["at"])
            if rec["type"] == "incident_opened":
                inc = {
                    "rule": body["rule"]["id"],
                    "active_since": self.offset(body["active_since"]),
                    "opened_at": at,
                    "resolving_since": None,
                    "resolved_at": None,
                    "routed_at": at if body["route"] == "routed" else None,
                }
                if body["group"]:
                    inc["group"] = {g["name"]: g["value"] for g in body["group"]}
                if "held_by_upgrade_until" in body:
                    inc["held_by_upgrade_until"] = self.offset(
                        body["held_by_upgrade_until"]
                    )
                out[body["incident_id"]] = inc
            elif rec["type"] == "incident_updated":
                inc, change = out[body["incident_id"]], body["change"]
                if change == "resolving":
                    inc["resolving_since"] = at
                elif change == "refiring":
                    inc["resolving_since"] = None
                elif change == "routed":
                    inc["routed_at"] = at
                elif change == "suppressed":
                    inc["suppressed_by"] = body["suppressed_by"]
                elif change == "reopened":  # §5 flapping: the same incident again
                    inc |= {"flapping": True, "reopen_count": body["reopen_count"]}
                    inc |= {"resolving_since": None, "resolved_at": None}
            elif rec["type"] == "incident_resolved":
                out[body["incident_id"]]["resolved_at"] = at
                out[body["incident_id"]]["reason"] = body["how"]
        return out


def run(vector: dict, *, refeed_history: bool = True) -> Run:
    start = datetime.strptime(vector["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    tick = secs(vector.get("tick", "15s"))
    restarts = {secs(r) for r in vector.get("engine_restarts", [])}
    checkpoints = {secs(c["at"]) for c in vector["expect"]}
    rules = rules_of(vector)
    # The install shape is the one the vector's sensors name (pack cases for
    # start_sh-only rules, F7c); E3's vectors name none, so Compose.
    named = {s["shape"] for s in vector["sensors"] if "shape" in s}
    if len(named) > 1:
        raise ValueError(f"sensors name more than one install shape: {sorted(named)}")
    shape = named.pop() if named else "compose"
    observations = expand(vector)
    observed = [
        (datetime.fromisoformat(o["observed_at"]) - start).total_seconds()
        for o in observations
    ]

    def fresh(state: dict | None) -> Engine:
        return Engine(
            rules,
            instance_id=INSTANCE,
            install_shape=shape,
            state=state,
            suppression=vector.get("suppression", []),
            group_cap=vector.get("limits", {}).get("group_cap", 64),
        )

    engine, fed, records, status = fresh(None), 0, [], {}
    for t in range(0, secs(vector["end"]) + 1, tick):
        if t in restarts:
            engine = fresh(json.loads(json.dumps(engine.state())))
            for i in range(fed if refeed_history else 0):
                engine.observe(observations[i])
        while fed < len(observations) and observed[fed] <= t:
            engine.observe(observations[fed])
            fed += 1
        records += engine.tick(start + timedelta(seconds=t))
        if t in checkpoints:
            for s in engine.status():
                status[(t, s["rule"], group_key(s["group"]))] = s
    return Run(records, status, start)
