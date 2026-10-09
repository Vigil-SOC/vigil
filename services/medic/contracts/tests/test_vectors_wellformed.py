"""E3 contract: evaluation vectors are well-formed and internally consistent.

The engine (E4) runs these vectors later. Here we check what can be checked without
an engine: structure, that the rules load, that the expanded observations are valid
D3, and that every expected timeline obeys semantics.md §4–§7.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from rule_check import check
from vector_expand import expand, secs

VECTORS = sorted((ROOT / "vectors").glob("v[0-9][0-9]-*.yaml"))
VSCHEMA = Draft202012Validator(json.loads((ROOT / "vector.schema.json").read_text()))
OBS = Draft202012Validator(json.loads((ROOT / "observation.schema.json").read_text()))
RULES_DIR = ROOT / "fixtures" / "rules" / "valid"
BUILTIN = {
    "watcher.sensor-blind": {
        "for": "2m",
        "keep_firing_for": "0s",
        "fault": {"class": "watcher"},
    }
}
DEFAULT_FOR, DEFAULT_KEEP, FLAP_CLEAR, CHILD_GRACE, TICK = 120, 300, 1800, 600, 15


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def rules_of(vector: dict) -> dict[str, dict]:
    out = {}
    for entry in vector["rules"]:
        rule = (
            yaml.safe_load((RULES_DIR / entry["ref"]).read_text())
            if "ref" in entry
            else entry["inline"]
        )
        out[rule["id"]] = rule
    return out


def selects(sel: dict, rule_id: str, rule: dict) -> bool:
    key, val = next(iter(sel.items()))
    fault = rule.get("fault", {})
    return {
        "rule": rule_id == val,
        "class": fault.get("class") == val,
        "mode": fault.get("mode") == val,
        "cause": val in fault.get("causes", []),
    }[key]


def is_child(vector: dict, rule_id: str, rule: dict) -> bool:
    return any(
        selects(s["children"], rule_id, rule) for s in vector.get("suppression", [])
    )


def open_at(incident: dict, t: int) -> bool:
    opened = secs(incident["opened_at"])
    resolved = incident["resolved_at"]
    return opened <= t and (resolved is None or t < secs(resolved))


@pytest.fixture(scope="module", params=VECTORS, ids=lambda p: p.stem)
def vector(request: pytest.FixtureRequest) -> dict:
    return load(request.param)


def test_enough_vectors_covering_every_section() -> None:
    assert len(VECTORS) >= 15
    covered = {c.split()[0] for p in VECTORS for c in load(p)["covers"]}
    assert {"§2", "§3", "§4", "§5", "§6", "§7"} <= covered


def test_ids_match_file_names() -> None:
    for p in VECTORS:
        assert load(p)["id"] == p.stem


def test_vector_schema(vector: dict) -> None:
    errors = [e.message for e in VSCHEMA.iter_errors(vector)]
    assert errors == []


def test_rules_load_and_params_are_in_bounds(vector: dict) -> None:
    for entry in vector["rules"]:
        rule = (
            yaml.safe_load((RULES_DIR / entry["ref"]).read_text())
            if "ref" in entry
            else entry["inline"]
        )
        res = check(rule)
        assert res.ok and res.skipped is None, res.errors
        for name, value in entry.get("params", {}).items():
            spec = rule["params"][name]
            lo, hi, v = (
                secs(x) if spec["type"] == "duration" else x
                for x in (spec["min"], spec["max"], value)
            )
            assert lo <= v <= hi


def test_every_rule_signal_has_a_sensor(vector: dict) -> None:
    covered = {sig for s in vector["sensors"] for sig in s["covers"]}
    for rule in rules_of(vector).values():
        for sig in rule["signals"].values():
            assert next(iter(sig.values()))["signal"] in covered


def test_expanded_observations_are_valid_d3(vector: dict) -> None:
    observations = expand(vector)
    assert observations
    for obs in observations:
        errors = [e.message for e in OBS.iter_errors(obs)]
        assert errors == [], (obs["id"], errors)


def test_times_are_on_the_tick_grid_and_in_range(vector: dict) -> None:
    end = secs(vector["end"])
    times = [c["at"] for c in vector["expect"]]
    for inc in vector["incidents"].values():
        times += [
            v
            for k, v in inc.items()
            if k.endswith(("_at", "_since", "_until")) and isinstance(v, str)
        ]
    for t in times:
        assert secs(t) % TICK == 0 and 0 <= secs(t) <= end, t


def test_checkpoints_obey_the_state_machine(vector: dict) -> None:
    rules = {**BUILTIN, **rules_of(vector)}
    incidents = vector["incidents"]
    for c in vector["expect"]:
        assert c["rule"] in rules, c
        ev, state = c["eval"], c["state"]
        assert (ev, state) not in {
            (True, "inactive"),
            (False, "pending"),
            (False, "firing"),
            (True, "resolving"),
        }, c
        group = c.get("group", {})
        if state in ("firing", "resolving"):
            inc = incidents[c["incident"]]
            assert inc["rule"] == c["rule"] and inc.get("group", {}) == group, c
            assert open_at(inc, secs(c["at"])), c
        else:
            assert "incident" not in c, c
            for inc in incidents.values():
                if (
                    inc["rule"] == c["rule"]
                    and inc.get("group", {}) == group
                    and not inc.get("flapping")
                ):
                    assert not open_at(inc, secs(c["at"])), c
        if "suppressed" in c:
            assert is_child(vector, c["rule"], rules[c["rule"]]), c


def test_incidents_respect_for_keep_suppression_and_routing(vector: dict) -> None:
    rules = {**BUILTIN, **rules_of(vector)}
    incidents = vector["incidents"]
    for label, inc in incidents.items():
        rule = rules[inc["rule"]]
        hold = secs(rule.get("for", "2m"))
        keep = (
            FLAP_CLEAR
            if inc.get("flapping")
            else secs(rule.get("keep_firing_for", "5m"))
        )
        active, opened = secs(inc["active_since"]), secs(inc["opened_at"])
        assert opened - active >= hold, label
        if "held_by_upgrade_until" in inc:
            assert opened >= secs(inc["held_by_upgrade_until"]), label
        if inc["resolved_at"] is not None and inc.get("reason") in (
            "group_retired",
            "rule_retired",
        ):
            # Retirement ends the incident at once, from firing or resolving, with no
            # false tick and no keep_firing_for (E3 §3, §4).
            assert secs(inc["resolved_at"]) >= secs(inc["opened_at"]), label
        elif inc["resolved_at"] is not None:
            assert inc["resolving_since"] is not None, label
            assert secs(inc["resolved_at"]) - secs(inc["resolving_since"]) >= keep, (
                label
            )
        child = is_child(vector, inc["rule"], rule)
        if "suppressed_by" in inc:
            parent = incidents[inc["suppressed_by"]]
            prule = rules[parent["rule"]]
            assert any(
                selects(s["parent"], parent["rule"], prule)
                and selects(s["children"], inc["rule"], rule)
                for s in vector.get("suppression", [])
            ), label
            assert (
                open_at(parent, opened) or secs(parent["opened_at"]) - opened <= 300
            ), label
            if inc.get("routed_at") is not None:
                assert parent["resolved_at"] is not None
                assert (
                    secs(inc["routed_at"]) >= secs(parent["resolved_at"]) + CHILD_GRACE
                ), label
        elif "routed_at" in inc and inc["routed_at"] is not None:
            expected = opened + (300 if child else 0)
            assert secs(inc["routed_at"]) == expected, label


def test_the_engine_vectors_are_found() -> None:
    assert (
        len(VECTORS) >= 26
    )  # v01–v22 (E3) + v23–v26 group retirement (X1 ⚑2a, S0 review)
