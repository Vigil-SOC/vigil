"""E3 vectors through the engine (semantics.md §8): every checkpoint and every
incident must match exactly, and nothing else may open.

Part 2 vectors (grouping cap, flapping, suppression, upgrade windows, group
retirement) are strict xfail: S4b2 has to flip each one when it lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from services.medic.contracts import decision_chain, lane_ref
from services.medic.contracts.vector_expand import load, secs
from services.medic.tests.engine.runner import (
    CONTRACTS,
    VECTORS,
    group_key,
    rules_of,
    run,
)

# Part 2 (S4b2). v15 (one group per source), v24 and v27 (groups that never retire)
# already hold on the part-1 engine, so they must pass now and keep passing.
PART2 = {f"v{n}" for n in (13, 14, 16, 17, 18, 19, 20, 23, 25, 26)}
RECORD = Draft202012Validator(
    json.loads((CONTRACTS / "decision-record.schema.json").read_text())
)


def _param(path: Path):
    marks = []
    if path.stem[:3] in PART2:
        marks = [pytest.mark.xfail(strict=True, reason="S4b2: engine core part 2")]
    return pytest.param(path, id=path.stem, marks=marks)


PARAMS = [_param(p) for p in VECTORS]


@pytest.mark.parametrize("path", PARAMS)
def test_vector(path: Path) -> None:
    vector = load(path)
    result = run(vector)
    actual = result.incidents()
    by_key = {
        (i["rule"], group_key(i.get("group", {})), i["active_since"]): (iid, i)
        for iid, i in actual.items()
    }
    problems: list[str] = []

    # Incidents: exactly the expected ones, field for field.
    label_to_id: dict[str, str] = {}
    for label, want in vector["incidents"].items():
        since = secs(want["active_since"])
        key = (want["rule"], group_key(want.get("group", {})), since)
        if key not in by_key:
            problems.append(f"incident {label} {key} never opened")
            continue
        iid, got = by_key.pop(key)
        label_to_id[label] = iid
        for field, value in want.items():
            if isinstance(value, str) and value.startswith("+"):
                value = secs(value)
            if got.get(field) != value:
                problems.append(
                    f"incident {label}.{field}: {got.get(field)} != {value}"
                )
    for key, (_, got) in by_key.items():
        problems.append(f"unexpected incident {key}: {got}")

    # Checkpoints: eval, state and which incident, at that tick.
    for c in vector["expect"]:
        got = result.status.get(
            (secs(c["at"]), c["rule"], group_key(c.get("group", {})))
        )
        where = f"{c['at']} {c['rule']} {c.get('group', {})}"
        if got is None:
            problems.append(f"{where}: no status")
            continue
        want_eval = {True: "true", False: "false"}.get(c["eval"], c["eval"])
        if (got["eval"], got["state"]) != (want_eval, c["state"]):
            problems.append(
                f"{where}: {got['eval']}/{got['state']} != {want_eval}/{c['state']}"
            )
        if "incident" in c and got["incident_id"] != label_to_id.get(c["incident"]):
            problems.append(f"{where}: not incident {c['incident']}")
    assert problems == []


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector_records_are_valid_decision_records(path: Path) -> None:
    """Completed with stub router fields (R1) and sealed, every record is valid G2."""
    vector = load(path)
    lanes = {r.rule["id"]: r.rule["lane"] for r in rules_of(vector)}
    prev = None
    for rec in run(vector).records:
        rec = {"v": 1, **rec}
        if rec["type"] == "incident_opened":
            body = rec["body"]
            lane = lanes.get(body["rule"]["id"], 3)
            inc = lane_ref.Incident(
                id=body["incident_id"], rule="r", lane=lane, opened_at=0
            )
            rec["body"] = {
                **body,
                "lane": lane_ref.lane_of(inc),
                "route": "routed",
                "runbook": None,
                "would_have": lane_ref.would_have(inc, lane),
            }
        prev = decision_chain.seal(rec, prev)
        errors = [e.message for e in RECORD.iter_errors(prev)]
        assert errors == [], (rec, errors)
    assert prev is not None or not vector["incidents"]


@pytest.mark.parametrize("path", [p for p in VECTORS if p.stem[:3] not in PART2])
def test_same_inputs_give_byte_identical_records(path: Path) -> None:
    vector = load(path)
    first, second = run(vector).records, run(vector).records
    canon = decision_chain.canonical
    assert canon(first) == canon(second)
