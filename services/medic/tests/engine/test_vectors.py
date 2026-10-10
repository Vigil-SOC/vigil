"""E3 vectors through the engine (semantics.md §8): every checkpoint and every
incident must match exactly, and nothing else may open.

A vector waiting on a contract fix is strict xfail with its ⚑ (CONTRACT below), so
the contract PR that fixes it has to flip it.
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

# Engine core part 2 (S4b2) landed: no vector is expected to fail.
PART2: set[str] = set()
# Vectors waiting on a contract decision (outputs/skeleton/S4b-notes.md, ⚑ list).
CONTRACT: dict[str, str] = {
    "v26": "⚑ S4b2-1: v26 omits the sensor-blind incident §2 requires (S4b2-notes)",
}
RECORD = Draft202012Validator(
    json.loads((CONTRACTS / "decision-record.schema.json").read_text())
)


def _param(path: Path):
    marks = []
    if path.stem[:3] in PART2:
        marks = [pytest.mark.xfail(strict=True, reason="S4b2: engine core part 2")]
    if path.stem[:3] in CONTRACT:
        marks = [pytest.mark.xfail(strict=True, reason=CONTRACT[path.stem[:3]])]
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
    found: dict[str, dict] = {}
    for label, want in vector["incidents"].items():
        since = secs(want["active_since"])
        key = (want["rule"], group_key(want.get("group", {})), since)
        if key not in by_key:
            problems.append(f"incident {label} {key} never opened")
            continue
        label_to_id[label], found[label] = by_key.pop(key)
    for label, got in found.items():
        for field, value in vector["incidents"][label].items():
            if isinstance(value, str) and value.startswith("+"):
                value = secs(value)
            if field == "suppressed_by":  # a label naming the parent incident
                value = label_to_id.get(value, value)
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
        if "suppressed" in c and got["suppressed"] != c["suppressed"]:
            problems.append(f"{where}: suppressed {got['suppressed']}")
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
                "runbook": None,
                "would_have": lane_ref.would_have(inc, lane),
            }
        prev = decision_chain.seal(rec, prev)
        errors = [e.message for e in RECORD.iter_errors(prev)]
        assert errors == [], (rec, errors)
    assert prev is not None or not vector["incidents"]


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_same_inputs_give_byte_identical_records(path: Path) -> None:
    vector = load(path)
    first, second = run(vector).records, run(vector).records
    canon = decision_chain.canonical
    assert canon(first) == canon(second)


NEXT = {  # incident lifecycle record -> the lifecycle records that may follow it
    "incident_opened": {"resolving"},
    "resolving": {"refiring", "incident_resolved"},
    "refiring": {"resolving"},
    "closed": {"reopened"},  # §5 flapping damping reopens the most recent incident
    "reopened": {"resolving"},
}
SIDE = {"evidence_added", "routed", "suppressed", "unsuppressed"}  # while open


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_record_sequences_are_legal(path: Path) -> None:
    """Each incident: opened once, legal transitions (§4, §5), routing facts only
    while open, routed at most once and never while suppressed (§6), and evidence
    for at most one observation per rule signal (decision-record.md §3, N4).
    Records don't name the rule signal, so that last check is a count."""
    vector = load(path)
    signals = {r.id: len(r.rule["signals"]) for r in rules_of(vector)}
    last: dict[str, str] = {}
    keys: set[tuple] = set()
    items: dict[str, int] = {}
    limit: dict[str, int] = {}
    routed: dict[str, bool] = {}
    suppressed: dict[str, bool] = {}
    for rec in run(vector).records:
        body = rec["body"]
        iid = body["incident_id"]
        if rec["type"] == "incident_opened":
            assert iid not in last, f"{iid} opened twice"
            key = (body["rule"]["id"], str(body["group"]), body["active_since"])
            assert key not in keys, f"{key} opened twice"
            keys.add(key)
            items[iid] = len(body["evidence"])
            limit[iid] = signals.get(body["rule"]["id"], 1)
            last[iid] = "incident_opened"
            routed[iid], suppressed[iid] = body["route"] == "routed", False
            continue
        step = body.get("change", rec["type"])
        if body.get("how") in ("rule_retired", "group_retired"):
            step = "retired"  # §4: ends an open incident at once, no resolving phase
            assert last.get(iid) not in (None, "closed"), (iid, step)
            last[iid] = "closed"
        elif step in SIDE:
            assert last.get(iid) not in (None, "closed"), (iid, step)
        else:
            assert step in NEXT.get(last.get(iid, ""), ()), (iid, last.get(iid), step)
            last[iid] = "closed" if step == "incident_resolved" else step
        if step == "evidence_added":
            items[iid] += len(body["evidence"])
        elif step == "suppressed":
            assert not routed[iid] and not suppressed[iid], (iid, step)
            suppressed[iid] = True
        elif step == "unsuppressed":
            assert suppressed[iid], (iid, step)
            suppressed[iid] = False
        elif step == "routed":
            assert not routed[iid] and not suppressed[iid], (iid, step)
            routed[iid] = True
        assert items[iid] <= limit[iid], f"{iid}: more evidence than rule signals"
