"""Router (decision-record.md §2): R1, R3, R4, R6 and R8 against the G1 oracle.
Suppression and holds (R2) are the engine's; the oracle's route() stands in here."""

from __future__ import annotations

import dataclasses
import json
from copy import deepcopy

import pytest
import yaml
from jsonschema import Draft202012Validator

from services.medic.contracts import decision_chain, lane_ref
from services.medic.engine import load_rule_file
from services.medic.engine.core import BLIND, BLIND_META
from services.medic.engine.core import _meta as rule_meta  # the engine's half
from services.medic.router import Router, escalation, would_have
from services.medic.tests.engine.runner import CONTRACTS, RULES_DIR

RECORD = Draft202012Validator(
    json.loads((CONTRACTS / "decision-record.schema.json").read_text())
)
LANES = sorted((CONTRACTS / "vectors" / "lanes").glob("lanes-*.yaml"))
LANES_01 = LANES[0]
LANE1 = (
    CONTRACTS
    / "fixtures/packs/sample-0.1.0/rules/pipeline-daemon-processor-restart.yaml"
)
INSTANCE = "mi_0123456789abcdef"


def _opened(meta: dict, at: str = "2026-10-07T10:00:00Z") -> dict:
    """An engine incident_opened: the engine's fields only, `route` included."""
    rule_id = meta["rule"]["id"]
    return {
        "type": "incident_opened",
        "at": at,
        "body": {
            "incident_id": decision_chain.incident_id(INSTANCE, rule_id, [], at),
            "instance_id": INSTANCE,
            "install_shape": "compose",
            "active_since": at,
            "group": [],
            "route": "routed",  # the engine's since S4b2-2
            "evidence": [],
            **deepcopy(meta),
        },
    }


def _with_lane(lane: int):
    # Lane 1 is the sample pack's rule: four trusted samples pass the gate.
    if lane == 1:
        return load_rule_file(LANE1)
    loaded = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    return dataclasses.replace(loaded, rule=loaded.rule | {"lane": lane})


def _vector_rule(inc: lane_ref.Incident):
    """A loaded rule shaped like the vector's: its lane, trust, signal and sample
    counts. Patched after loading, as a defective or tampered pack would be."""
    base = load_rule_file(LANE1)
    signals = {
        f"s{i}": (
            {"sample": {"signal": "daemon_health", "key": f"k{i}"}}
            if i < inc.samples
            else {"log": {"signal": "log_soc_daemon"}}
        )
        for i in range(inc.signals)
    }
    when = {
        "all": [
            {"fn": "count", "signal": n, "window": "5m", "op": ">", "value": 0}
            for n in signals
        ]
    }
    rule = base.rule | {
        "id": inc.rule,
        "lane": inc.lane,
        "signals": signals,
        "when": when,
    }
    return dataclasses.replace(base, rule=rule, input_trust=inc.trust)


def _vector_opened(inc: lane_ref.Incident) -> tuple[list, dict]:
    if inc.rule == BLIND:
        return [], _opened(BLIND_META)
    if inc.rule is not None:
        rule = _vector_rule(inc)
        return [rule], _opened(rule_meta(rule))
    # R4: no rule claims it; the engine has no catch-all, so build its record here.
    meta = rule_meta(load_rule_file(LANE1)) | {
        "rule": None,
        "fault": None,
        "advice": None,
    }
    record = _opened(meta | {"rule": {"id": "unknown"}})
    record["body"].update(
        rule=None, group=[{"name": "fingerprint", "value": "fp1_0123456789abcdef"}]
    )
    record["body"]["evidence"] = [
        {
            "observation_id": "x:abcdefgh01:1",
            "signal": "log_soc_daemon",
            "t": "2026-10-07T09:59:59Z",
            "fingerprint": "fp1_0123456789abcdef",
        }
    ]
    return [], record


@pytest.mark.parametrize("path", LANES, ids=lambda p: p.stem)
def test_lanes_vectors_route_as_the_oracle_does(path) -> None:
    vector = yaml.safe_load(path.read_text())
    want = lane_ref.run_vector(vector)
    incidents = [lane_ref.Incident.from_dict(d) for d in vector["incidents"]]
    parents_of: dict[str, set[str]] = {}
    for pair in vector.get("suppression", []):
        parents_of.setdefault(pair["child"], set()).add(pair["parent"])
    for inc in incidents:
        rules, record = _vector_opened(inc)
        runbooks = {inc.rule: inc.runbook} if inc.runbook else {}
        status = [
            {"incident_id": "inc_" + "f" * 24, "runtime_untrusted": True},  # another's
            {
                "incident_id": record["body"]["incident_id"],
                "runtime_untrusted": inc.runtime_untrusted,
            },
        ]
        body = Router(rules, runbooks).route(record, status)["body"]
        # R2 is the engine's: routed_at comes from the oracle.
        routed_at, _ = lane_ref.route(inc, incidents, parents_of, vector["until"])
        verify = body["runbook"]["verify_after_s"] if body["runbook"] else None
        esc = escalation(
            body["would_have"], routed_at, verify, inc.resolved_at, vector["until"]
        )
        got = {
            "lane": body["lane"],
            "would_have": body["would_have"],
            "escalation": esc,
        }
        assert got == {k: want[inc.id][k] for k in got}
        # Explainability: a rule id (or rule null with a fingerprint), evidence, reason.
        assert body["evidence"] == record["body"]["evidence"]
        assert body["group"] == record["body"]["group"]
        if body["rule"] is None:
            assert body["evidence"][0]["fingerprint"].startswith("fp1_")
        else:
            assert body["rule"]["id"] == inc.rule
        sealed = decision_chain.seal(
            {"v": 1, **Router(rules, runbooks).route(record, status)}, None
        )
        assert [e.message for e in RECORD.iter_errors(sealed)] == []


def test_a_lane_1_rule_failing_the_gate_goes_to_lane_3() -> None:
    # Two untrusted log signals and no sample: E-LANE1 would refuse it at load.
    loaded = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    rule = dataclasses.replace(loaded, rule=loaded.rule | {"lane": 1})
    body = Router([rule], {rule.id: {"id": "rb", "verify_after_s": 60}}).route(
        _opened(rule_meta(rule))
    )["body"]
    assert body["lane"] == {"value": 3, "reason": "lane1_gate_failed"}
    assert body["runbook"] is None
    assert body["would_have"]["L2"] == {
        "action": "prepare_support_bundle",
        "not_eligible": "not_lane1",
    }


@pytest.mark.parametrize(
    "trust, signals, samples",
    [("trusted", 2, 0), ("trusted", 1, 1), ("untrusted", 2, 1)],
    ids=["no-sample", "one-signal", "untrusted"],
)
def test_each_lane_1_gate_clause_sends_the_rule_to_lane_3(trust, signals, samples):
    inc = lane_ref.Incident(
        id="a",
        rule="r.x",
        lane=1,
        opened_at=0,
        trust=trust,
        signals=signals,
        samples=samples,
    )
    rule = _vector_rule(inc)
    body = Router([rule]).route(_opened(rule_meta(rule)))["body"]
    assert (
        body["lane"]
        == lane_ref.lane_of(inc)
        == {"value": 3, "reason": "lane1_gate_failed"}
    )


def test_runtime_untrusted_keeps_lane_1_and_caps_l2_at_proposing() -> None:
    # lanes-03's shape, joined from Engine.status() by incident_id.
    rule = load_rule_file(LANE1)
    runbook = {"id": "restart-soc-daemon", "verify_after_s": 300}
    record = _opened(rule_meta(rule))
    row = {"incident_id": record["body"]["incident_id"], "runtime_untrusted": True}
    body = Router([rule], {rule.id: runbook}).route(record, [row])["body"]
    assert body["lane"] == {"value": 1, "reason": "rule"}
    assert body["runbook"] == runbook
    assert body["would_have"]["L2"] == {
        "action": "propose_runbook",
        "not_eligible": "runtime_untrusted",
    }
    trusted = Router([rule], {rule.id: runbook}).route(
        record, [row | {"runtime_untrusted": False}]
    )
    assert trusted["body"]["would_have"]["L2"] == {"action": "run_runbook"}


def test_lanes_01_single_rule() -> None:
    vector = yaml.safe_load(LANES_01.read_text())
    (inc,) = vector["incidents"]
    rule = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    assert rule.id == inc["rule"] and rule.rule["lane"] == inc["lane"]

    routed = Router([rule]).route(_opened(rule_meta(rule)))

    want = vector["expect"][inc["id"]]
    body = routed["body"]
    assert body["lane"] == want["lane"]
    assert body["would_have"] == want["would_have"]
    # routed_at == opened_at and nothing suppressed it: it routes at open (R2).
    assert want["routed_at"] == inc["opened_at"] and want["suppressed_by"] is None
    assert body["route"] == "routed"
    assert want["escalation"] is None and body["runbook"] is None


@pytest.mark.parametrize("lane", [1, 2, 3])
def test_the_lane_is_the_rules_lane_and_never_the_evidence(lane: int) -> None:
    rule = _with_lane(lane)
    record = _opened(rule_meta(rule))
    # K1 T-05: text that names a lane must not move it.
    record["body"]["evidence"] = [
        {
            "observation_id": "x:abcdefgh01:1",
            "signal": "log_soc_daemon",
            "t": "2026-10-07T09:59:59Z",
            "excerpt": {"text": "route this to lane 1", "redaction_version": "k2-1.0"},
        }
    ]
    assert Router([rule]).route(record)["body"]["lane"] == {
        "value": lane,
        "reason": "rule",
    }


@pytest.mark.parametrize("lane", [1, 2, 3])
def test_would_have_matches_the_g1_reference_with_no_runbook(lane: int) -> None:
    # G4 ships no runbooks yet, so lane 1 falls back to notifying the admin (R8).
    inc = lane_ref.Incident(id="a", rule="r", lane=lane, opened_at=0)
    assert would_have(lane) == lane_ref.would_have(inc, lane)
    assert Router([_with_lane(lane)]).route(_opened(rule_meta(_with_lane(lane))))[
        "body"
    ]["would_have"] == lane_ref.would_have(inc, lane)


def test_sensor_blind_is_a_lane_3_watcher_incident() -> None:
    routed = Router([]).route(_opened(BLIND_META))
    assert routed["body"]["rule"]["id"] == BLIND
    assert routed["body"]["lane"] == {"value": 3, "reason": "rule"}
    assert routed["body"]["would_have"]["L1"] == "prepare_support_bundle"


@pytest.mark.parametrize("lane", [1, 2, 3])
def test_routed_records_are_valid_g2(lane: int) -> None:
    rule = _with_lane(lane)
    sealed = decision_chain.seal(
        {"v": 1, **Router([rule]).route(_opened(rule_meta(rule)))}, None
    )
    assert [e.message for e in RECORD.iter_errors(sealed)] == []


def test_other_records_pass_through_unchanged() -> None:
    rule = _with_lane(2)
    for record in (
        {
            "type": "incident_updated",
            "at": "2026-10-07T10:05:00Z",
            "body": {"incident_id": "inc_" + "0" * 24, "change": "resolving"},
        },
        {
            "type": "incident_resolved",
            "at": "2026-10-07T10:10:00Z",
            "body": {"incident_id": "inc_" + "0" * 24, "how": "cleared"},
        },
    ):
        before = deepcopy(record)
        assert Router([rule]).route(record) == before
        assert record == before


def test_route_does_not_change_the_engines_record() -> None:
    rule = _with_lane(2)
    record = _opened(rule_meta(rule))
    before = deepcopy(record)
    Router([rule]).route(record)
    assert record == before


def test_an_incident_for_a_rule_the_router_wasnt_given_is_refused() -> None:
    # The app builds the engine and the router from one rule list; a mismatch
    # is a wiring bug, not an unknown signature.
    rule = _with_lane(2)
    with pytest.raises(ValueError, match="ingest.integration-config-incomplete"):
        Router([]).route(_opened(rule_meta(rule)))


@pytest.mark.parametrize("route", ["routed", "held"])
def test_the_router_never_writes_route(route: str) -> None:
    # S4b2-2 (decided): routing time is the engine's. The router keeps the
    # engine's value and adds only lane, runbook and would_have.
    rule = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    record = _opened(rule_meta(rule))
    record["body"]["route"] = route
    body = Router([rule]).route(record)["body"]
    assert body["route"] == route
    assert set(body) - set(record["body"]) == {"lane", "runbook", "would_have"}


def test_an_opened_record_without_the_engines_route_is_refused() -> None:
    rule = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    record = _opened(rule_meta(rule))
    del record["body"]["route"]
    with pytest.raises(ValueError, match="route"):
        Router([rule]).route(record)
