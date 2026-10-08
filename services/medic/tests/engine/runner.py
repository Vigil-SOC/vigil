"""Drive the engine through an E3 vector (semantics.md §8) on a fake clock.

The runner expands the vector into D3 observations with the contract's own
expander, feeds them in observed_at order, ticks every 15 s from start to end, and
keeps every record the engine emits plus its status at each checkpoint tick. An
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
                    # Routing is G3's; with no suppression and no upgrade window
                    # (part 1) an incident routes when it opens (E3 §6.4).
                    "routed_at": at,
                }
                if body["group"]:
                    inc["group"] = {g["name"]: g["value"] for g in body["group"]}
                out[body["incident_id"]] = inc
            elif rec["type"] == "incident_updated":
                if body["change"] == "resolving":
                    out[body["incident_id"]]["resolving_since"] = at
                elif body["change"] == "refiring":
                    out[body["incident_id"]]["resolving_since"] = None
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
    observations = expand(vector)
    observed = [
        (datetime.fromisoformat(o["observed_at"]) - start).total_seconds()
        for o in observations
    ]

    def fresh(state: dict | None) -> Engine:
        return Engine(rules, instance_id=INSTANCE, install_shape="compose", state=state)

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
