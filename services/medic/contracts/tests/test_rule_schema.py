"""E2 contract: rule schema v1 and the engine API 1.0 loader checks."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from rule_check import check, check_text

RULES = ROOT / "fixtures" / "rules"
VALID = sorted((RULES / "valid").glob("*.yaml"))
INVALID = sorted((RULES / "invalid").glob("*.yaml"))
UNTRUSTED = {"ingest.integration-config-incomplete", "llm.gateway-outage"}
E1_IDS = {
    "ingest.source-silent",
    "ingest.kafka-decode-errors",
    "ingest.integration-config-incomplete",
    "pipeline.daemon-component-hung",
    "llm.gateway-flapping",
}


def _rule(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(
        json.loads((ROOT / "rule.schema.json").read_text())
    )


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_rule_loads(path: Path) -> None:
    res = check_text(path.read_text())
    assert res.ok, res.errors
    assert res.skipped is None


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_input_trust_is_derived(path: Path) -> None:
    rule = _rule(path)
    expected = "untrusted" if rule["id"] in UNTRUSTED else "trusted"
    assert check(rule).input_trust == expected


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_rule_is_refused_with_its_code(path: Path) -> None:
    text = path.read_text()
    expected = text.splitlines()[0].removeprefix("# expect: ").strip()
    assert check_text(text).codes == {expected}


def test_coverage_of_v1_fault_classes_and_e1_examples() -> None:
    rules = [_rule(p) for p in VALID]
    assert len(rules) >= 8
    assert {r["fault"]["class"] for r in rules} == {"ingest", "llm", "pipeline"}
    assert E1_IDS <= {r["id"] for r in rules}
    assert len(INVALID) >= 5


def test_oversized_rule_is_refused() -> None:
    text = VALID[0].read_text() + "#" + "x" * 17_000
    assert check_text(text).codes == {"E-SIZE"}


def test_rule_needing_newer_engine_is_skipped_not_refused() -> None:
    rule = _rule(RULES / "valid" / "ingest-kafka-decode-errors.yaml")
    rule["engine_api"] = "1.3"
    res = check(rule)
    assert res.ok
    assert res.skipped and "1.3" in res.skipped


def test_rule_needing_another_major_is_refused() -> None:
    rule = _rule(RULES / "valid" / "ingest-kafka-decode-errors.yaml")
    rule["engine_api"] = "2.0"
    assert check(rule).codes == {"E-SCHEMA"}


def test_lane1_gate_accepts_corroborated_trusted_rule() -> None:
    rule = copy.deepcopy(_rule(RULES / "valid" / "pipeline-daemon-component-hung.yaml"))
    rule["lane"] = 1
    res = check(rule)
    assert res.ok, res.errors
    assert res.input_trust == "trusted"


def test_lane1_gate_refuses_untrusted_rule() -> None:
    rule = copy.deepcopy(_rule(RULES / "valid" / "llm-gateway-outage.yaml"))
    rule["lane"] = 1
    assert "E-LANE1" in check(rule).codes


def test_trusted_only_false_taints_the_rule() -> None:
    rule = copy.deepcopy(
        _rule(RULES / "valid" / "ingest-integration-config-incomplete.yaml")
    )
    for sig in rule["signals"].values():
        sig["log"].pop("keys")
    rule.pop("group_by")
    assert check(rule).input_trust == "trusted"
    rule["signals"]["incomplete"]["log"]["trusted_only"] = False
    assert check(rule).input_trust == "untrusted"


# X1 ⚑4a (decided 2026-10-07): detection type is declared by the rule, default event.
def test_detection_defaults_to_event() -> None:
    rule = _rule(VALID[0])
    rule.pop("detection", None)
    assert check(rule).detection == "event"


def test_detection_absence_is_carried_through() -> None:
    rule = _rule(RULES / "valid" / "ingest-source-silent.yaml")
    rule["detection"] = "absence"
    res = check(rule)
    assert res.ok, res.errors
    assert res.detection == "absence"


def test_detection_outside_event_or_absence_is_refused() -> None:
    rule = _rule(VALID[0])
    rule["detection"] = "both"
    assert check(rule).codes == {"E-SCHEMA"}


# S0 review R7: a rule that waits out a quiet gap must say so, or A4's TTD split is wrong.
ABSENCE_RULES = {
    "ingest.source-never-polled",
    "ingest.source-silent",
    "pipeline.triage-stalled",
    "pipeline.daemon-component-hung",
}


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_absence_rules_declare_it(path: Path) -> None:
    rule = _rule(path)
    want = "absence" if rule["id"] in ABSENCE_RULES else "event"
    assert check(rule).detection == want


@pytest.mark.parametrize(
    "node",
    [
        {"fn": "absent_for", "signal": "polls", "window": "30m"},
        {
            "fn": "increase",
            "signal": "arrivals",
            "window": "1h",
            "op": "==",
            "value": 0,
        },
        {"fn": "changes", "signal": "polls", "window": "30m", "op": "<=", "value": 0},
    ],
)
def test_an_undeclared_absence_condition_is_refused(node: dict) -> None:
    rule = _rule(RULES / "valid" / "ingest-source-never-polled.yaml")
    rule.pop("detection", None)
    rule["when"] = node
    assert "E-DETECTION" in check(rule).codes


# S0 re-check N2 (decided 2026-10-07): the field stays declared, not derived (⚑4a). The loader
# only refuses a rule that waits out a quiet gap and says nothing; an explicit choice stands.
def test_an_explicit_event_declaration_is_accepted() -> None:
    rule = _rule(RULES / "valid" / "ingest-source-never-polled.yaml")
    rule["detection"] = "event"
    res = check(rule)
    assert "E-DETECTION" not in res.codes
    assert res.detection == "event"


def test_a_negated_absence_condition_is_not_absence() -> None:
    rule = _rule(RULES / "valid" / "ingest-source-never-polled.yaml")
    rule.pop("detection")
    rule["when"] = {"not": {"fn": "absent_for", "signal": "polls", "window": "30m"}}
    assert "E-DETECTION" not in check(rule).codes
