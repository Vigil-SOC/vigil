"""The skeleton's dev-mode rules load through the S4b loader (no pack loader, F6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.medic.app.cli import main
from services.medic.app.wiring import (
    DEV_RULES_DIR,
    load_dev_rules,
    load_dev_suppression,
)
from services.medic.sensors.http_ready import agent_worker_ready


def test_the_dev_rule_set_is_the_one_readyz_rule() -> None:
    rules = load_dev_rules()
    assert [r.id for r in rules] == ["pipeline.agent-worker-not-ready"]
    assert sorted(p.name for p in DEV_RULES_DIR.iterdir()) == [
        "pipeline-agent-worker-not-ready.yaml",
        "suppression.yaml",
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


def test_the_dev_suppression_set_is_empty_and_a_missing_file_is_none(
    tmp_path: Path,
) -> None:
    assert load_dev_suppression() == []
    assert load_dev_suppression(tmp_path / "suppression.yaml") == []


def test_suppression_entries_are_read_as_written(tmp_path: Path) -> None:
    path = tmp_path / "suppression.yaml"
    path.write_text("- {parent: {class: llm}, children: {mode: P-4}}\n")
    assert load_dev_suppression(path) == [
        {"parent": {"class": "llm"}, "children": {"mode": "P-4"}}
    ]
    path.write_text("parent: {class: llm}\n")
    with pytest.raises(TypeError, match="list"):
        load_dev_suppression(path)


def test_a_malformed_suppression_entry_stops_medic_before_it_beats(
    tmp_path: Path, monkeypatch
) -> None:
    # The engine refuses it (E3 §6); Medic exits 1 and writes no beat or gap.
    from services.medic.app import cli

    bad = [{"parent": {"tier": "x"}, "children": {"mode": "P-4"}}]
    monkeypatch.setattr(cli, "load_dev_suppression", lambda: bad)
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(["run"], env=env, max_cycles=1) == 1
    assert not (tmp_path / "run" / "heartbeat").exists()
