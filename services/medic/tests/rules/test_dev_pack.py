"""Dev mode runs the bundled medic-core pack straight from its source tree, through
the S4b loader: no signatures, no catalog (F6 loads the signed pack). F7c retired
the skeleton's `rules/dev/` set; its /readyz rule now lives in the pack."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.medic.app.cli import main
from services.medic.app.wiring import (
    PACK_SOURCE,
    load_pack_rules,
    load_pack_suppression,
)
from services.medic.sensors.http_ready import agent_serve_ready, agent_worker_ready

READYZ_RULES = {
    "pipeline.agent-worker-not-ready": agent_worker_ready(),
    "pipeline.agent-serve-not-ready": agent_serve_ready(),
}


def test_the_dev_rule_folder_is_gone_and_the_pack_is_loaded_whole() -> None:
    assert not (PACK_SOURCE.parents[1] / "rules" / "dev").exists()
    rules = load_pack_rules()
    ids = [r.id for r in rules]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert sorted(p.stem for p in (PACK_SOURCE / "rules").glob("*.yaml")) == sorted(
        i.replace(".", "-") for i in ids
    )
    assert all(r.skipped is None for r in rules)
    assert set(READYZ_RULES) <= set(ids)


def test_the_worker_readyz_rule_holds_2m_and_routes_to_lane_3() -> None:
    (rule,) = [
        r for r in load_pack_rules() if r.id == "pipeline.agent-worker-not-ready"
    ]
    assert rule.input_trust == "trusted"
    assert rule.seconds("for", 0) == 120
    assert rule.rule["lane"] == 3
    assert rule.rule["fault"]["mode"] == "P-1"


@pytest.mark.parametrize("rule_id", sorted(READYZ_RULES))
def test_the_readyz_rules_read_what_the_readyz_sensors_write(rule_id: str) -> None:
    (rule,) = [r for r in load_pack_rules() if r.id == rule_id]
    reads = {s["sample"]["signal"] for s in rule.rule["signals"].values()}
    assert reads == set(READYZ_RULES[rule_id].covers)


def test_the_pack_suppression_is_read_without_its_why_notes() -> None:
    entries = load_pack_suppression()
    assert entries, "the pack ships suppression entries"
    assert all(set(e) <= {"parent", "children", "match"} for e in entries)


def test_a_pack_without_suppression_has_none(tmp_path: Path) -> None:
    assert load_pack_suppression(tmp_path) == []
    (tmp_path / "suppression.yaml").write_text(
        "apiVersion: medic.suppression/v1\nkind: Suppression\nentries: []\n"
    )
    assert load_pack_suppression(tmp_path) == []


def test_suppression_entries_are_read_as_written(tmp_path: Path) -> None:
    path = tmp_path / "suppression.yaml"
    path.write_text(
        "apiVersion: medic.suppression/v1\nkind: Suppression\nentries:\n"
        "  - {parent: {class: llm}, children: {mode: P-4}, why: LLM first}\n"
    )
    assert load_pack_suppression(tmp_path) == [
        {"parent": {"class": "llm"}, "children": {"mode": "P-4"}}
    ]
    path.write_text("- {parent: {class: llm}, children: {mode: P-4}}\n")
    with pytest.raises(TypeError, match="entries"):
        load_pack_suppression(tmp_path)


def test_suppression_is_read_as_strictly_as_a_rule(tmp_path: Path) -> None:
    # Dev mode must not load a file F6's loader would refuse.
    import yaml

    path = tmp_path / "suppression.yaml"
    path.write_text(
        "apiVersion: medic.suppression/v1\nkind: Suppression\nentries:\n"
        "  - {parent: {class: llm}, parent: {class: ingest}, children: {mode: P-4}}\n"
    )
    with pytest.raises(yaml.YAMLError, match="E-YAML-DUPKEY"):
        load_pack_suppression(tmp_path)


def test_a_malformed_suppression_entry_stops_medic_before_it_beats(
    tmp_path: Path, monkeypatch
) -> None:
    # The engine refuses it (E3 §6); Medic exits 1 and writes no beat or gap.
    from services.medic.app import cli

    bad = [{"parent": {"tier": "x"}, "children": {"mode": "P-4"}}]
    monkeypatch.setattr(cli, "load_pack_suppression", lambda: bad)
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(["run"], env=env, max_cycles=1) == 1
    assert not (tmp_path / "run" / "heartbeat").exists()
