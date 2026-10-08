"""Router R1 (decision-record.md §2): the lane is the rule's lane, plus G2's
"would have" fields. Suppression, holds and simulated escalation are G3's."""

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
from services.medic.router import Router, would_have
from services.medic.tests.engine.runner import CONTRACTS, RULES_DIR

RECORD = Draft202012Validator(
    json.loads((CONTRACTS / "decision-record.schema.json").read_text())
)
LANES_01 = CONTRACTS / "vectors" / "lanes" / "lanes-01-single-rule.yaml"
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
    # A one-signal log rule can't load as lane 1 (E-LANE1); the router only reads
    # the lane, so lane 1 is set after loading.
    loaded = load_rule_file(RULES_DIR / "ingest-integration-config-incomplete.yaml")
    return dataclasses.replace(loaded, rule=loaded.rule | {"lane": lane})


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
