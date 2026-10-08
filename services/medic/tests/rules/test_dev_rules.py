"""The skeleton's dev-mode rules load through the S4b loader (no pack loader, F6)."""

from __future__ import annotations

from services.medic.app.wiring import DEV_RULES_DIR, load_dev_rules
from services.medic.sensors.http_ready import agent_worker_ready


def test_the_dev_rule_set_is_the_one_readyz_rule() -> None:
    rules = load_dev_rules()
    assert [r.id for r in rules] == ["pipeline.agent-worker-not-ready"]
    assert sorted(p.name for p in DEV_RULES_DIR.iterdir()) == [
        "pipeline-agent-worker-not-ready.yaml"
    ]


def test_the_readyz_rule_holds_2m_and_routes_to_lane_3() -> None:
    (rule,) = load_dev_rules()
    assert rule.skipped is None and rule.input_trust == "trusted"
    assert rule.seconds("for", 0) == 120
    assert rule.rule["lane"] == 3
    assert rule.rule["fault"]["mode"] == "P-1"


def test_the_rule_reads_what_the_readyz_sensor_writes() -> None:
    (rule,) = load_dev_rules()
    sensor = agent_worker_ready()
    reads = {s["sample"]["signal"] for s in rule.rule["signals"].values()}
    assert reads == set(sensor.covers)
