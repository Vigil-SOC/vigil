"""Engine behaviour the vectors don't pin on their own."""

from __future__ import annotations

import copy
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from services.medic.contracts.vector_expand import expand, load
from services.medic.engine import Engine, load_rule
from services.medic.engine.evaluate import UNKNOWN, decide, k_all, k_any, k_not
from services.medic.tests.engine.runner import (
    CONTRACTS,
    INSTANCE,
    group_key,
    rules_of,
    run,
)

T, F, U = True, False, UNKNOWN
VECTORS = CONTRACTS / "vectors"


def test_kleene_tables() -> None:
    assert [k_all([a, b]) for a, b in [(T, T), (T, U), (F, U), (U, U)]] == [T, U, F, U]
    assert [k_any([a, b]) for a, b in [(F, F), (F, U), (T, U), (U, U)]] == [F, U, T, U]
    assert [k_not(x) for x in (T, F, U)] == [F, T, U]


def test_latch_survives_a_restart_with_no_raw_history() -> None:
    """The latch lives in the state the caller persists, not in the raw history."""
    vector = load(VECTORS / "v10-latch-survives-restart.yaml")
    result = run(vector, refeed_history=False)
    group = group_key({"integration": "Elastic"})
    s = result.status[(915, "ingest.integration-config-incomplete", group)]
    assert (s["eval"], s["state"]) == ("true", "firing")
    assert [i["resolved_at"] for i in result.incidents().values()] == [35 * 60]


def test_restart_doesnt_reopen_or_re_mint_the_incident() -> None:
    vector = load(VECTORS / "v10-latch-survives-restart.yaml")
    opened = [r for r in run(vector).records if r["type"] == "incident_opened"]
    assert len(opened) == 1


def test_unknown_while_resolving_never_resolves() -> None:
    """No vector pins this (§4: "a blind watcher never auto-resolves"): v01 with the
    sensor stopped from +40m to +70m, inside keep_firing_for."""
    vector = load(VECTORS / "v01-hold-for-fires-and-resolves.yaml")
    vector["end"] = "+90m"
    vector["sensors"][0]["states"] = [
        ["+0s", "ok"],
        ["+40m", "stopped"],
        ["+70m", "ok"],
    ]
    result = run(vector)
    [incident] = [
        i for i in result.incidents().values() if i["rule"] != "watcher.sensor-blind"
    ]
    assert incident["resolving_since"] == 35 * 60
    assert incident["resolved_at"] == 85 * 60  # first covered false tick after +70m


def _rule(**over) -> dict:
    rule = yaml.safe_load(
        (CONTRACTS / "fixtures/rules/valid/ingest-kafka-decode-errors.yaml").read_text()
    )
    rule.update(over)
    return rule


def _engine(*rules: dict, shape: str = "compose") -> Engine:
    return Engine(
        [load_rule(r) for r in rules], instance_id=INSTANCE, install_shape=shape
    )


AT = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def test_skipped_and_off_shape_rules_are_never_healthy() -> None:
    engine = _engine(
        _rule(engine_api="1.3"),
        _rule(id="ingest.helm-only", shapes=["helm"]),
    )
    assert engine.tick(AT) == []
    status = {s["rule"]: s for s in engine.status()}
    assert status["ingest.kafka-decode-errors"]["eval"] == "unknown"
    assert status["ingest.kafka-decode-errors"]["not_evaluated"] == "needs_engine_minor"
    assert status["ingest.helm-only"]["not_evaluated"] == "shape"


def test_tick_must_be_on_the_grid() -> None:
    with pytest.raises(ValueError):
        _engine(_rule()).tick(AT.replace(second=7))


def test_instance_id_must_be_random_shaped() -> None:
    with pytest.raises(ValueError):
        Engine([], instance_id="my-host", install_shape="compose")


def _outage_engine() -> Engine:
    path = CONTRACTS / "fixtures/rules/valid/llm-gateway-outage.yaml"
    return Engine(
        [load_rule(yaml.safe_load(path.read_text()))],
        instance_id=INSTANCE,
        install_shape="compose",
    )


def test_observations_from_after_the_tick_are_not_used() -> None:
    engine = _outage_engine()
    for obs in expand(load(VECTORS / "v11-latch-reopens.yaml")):  # all 25 min at once
        engine.observe(obs)
    engine.tick(AT.replace(minute=1))
    assert engine.status()[0]["state"] == "inactive"  # the +2m set line isn't seen yet
    engine.tick(AT.replace(minute=2))
    assert engine.status()[0]["state"] == "pending"


def test_tick_must_move_forward_and_be_utc() -> None:
    engine = _engine(_rule())
    engine.tick(AT.replace(minute=5))
    with pytest.raises(ValueError):
        engine.tick(AT)
    with pytest.raises(ValueError):
        engine.tick(AT.replace(minute=6, tzinfo=None))
    restored = Engine(
        [], instance_id=INSTANCE, install_shape="compose", state=engine.state()
    )
    with pytest.raises(ValueError):
        restored.tick(AT)  # the last tick is part of the persisted state


def test_not_equal_on_a_lower_bound() -> None:
    assert decide("!=", 1, 5, covered=False) is UNKNOWN  # a larger y may equal 5
    assert decide("!=", 6, 5, covered=False) is True


def test_a_vanished_series_is_unknown_not_cleared() -> None:
    """v23 at +12h: the group's series stopped (its source was deleted) while the
    sensor still reads other sources. That is unknown, so the incident stays open
    (§4 never resolve blind); retirement itself is S4b2's."""
    vector = load(VECTORS / "v23-group-retired.yaml")
    vector["end"] = "+12h"
    result = run(vector)
    s = result.status[
        (12 * 3600, "ingest.source-erroring", group_key({"source": "gone"}))
    ]
    assert (s["eval"], s["state"]) == ("unknown", "firing")


def _v21_with(value, vtype: str) -> dict:
    vector = copy.deepcopy(load(VECTORS / "v21-kleene-any-all.yaml"))
    vector["types"]["redis_queues"]["bull_agent_runs.failed"] = vtype
    vector["series"][0]["steps"] = [["+0s", value]]
    return vector


def test_a_value_of_the_wrong_type_is_unknown_not_a_crash() -> None:
    result = run(_v21_with("lots", "gauge"))
    s = result.status[(60, "pipeline.failures-any", ())]
    assert (s["eval"], s["state"]) == ("unknown", "inactive")


def test_an_untrusted_value_taints_the_evaluation() -> None:
    vector = _v21_with("x", "text")
    vector["rules"][0]["inline"]["when"]["any"][0] |= {"op": "==", "value": "x"}
    s = run(vector).status[(60, "pipeline.failures-any", ())]
    assert (s["eval"], s["runtime_untrusted"]) == ("true", True)
    clean = run(load(VECTORS / "v21-kleene-any-all.yaml")).status
    assert clean[(60, "pipeline.failures-any", ())]["runtime_untrusted"] is False


def test_an_all_absent_gauge_has_no_lower_bound() -> None:
    vector = _v21_with(None, "gauge")
    rule = copy.deepcopy(vector["rules"][0]["inline"])
    rule["signals"] = {"failed": rule["signals"]["failed"]}
    rule["when"] = {"fn": "increase", "signal": "failed", "window": "5m"}
    rule["when"] |= {"op": "<", "value": -1}
    vector["rules"] = [{"inline": rule}]
    s = run(vector).status[(60, "pipeline.failures-any", ())]
    assert s["eval"] == "unknown"


def test_sensor_blind_with_no_heartbeat_yet_is_unknown_not_a_crash() -> None:
    """Restored state names a sensor whose heartbeats the new process hasn't seen
    (no raw history, or the sensor was removed)."""
    engine = _engine(_rule())
    observations = expand(load(VECTORS / "v05-unknown-never-fires.yaml"))
    engine.observe(next(o for o in observations if o["kind"] == "sensor_health"))
    engine.tick(AT)
    after = Engine(
        [], instance_id=INSTANCE, install_shape="compose", state=engine.state()
    )
    after.tick(AT.replace(second=15))
    [s] = after.status()
    assert (s["rule"], s["eval"]) == ("watcher.sensor-blind", "unknown")


def test_a_rule_removed_while_firing_resolves_rule_retired() -> None:
    vector = load(VECTORS / "v01-hold-for-fires-and-resolves.yaml")
    vector["end"] = "+31m"
    engine = Engine(rules_of(vector), instance_id=INSTANCE, install_shape="compose")
    for obs in expand(vector):
        engine.observe(obs)
    for t in range(0, 31 * 60 + 1, 15):
        engine.tick(AT + timedelta(seconds=t))
    assert engine.status()[0]["state"] == "firing"
    after = Engine(
        [], instance_id=INSTANCE, install_shape="compose", state=engine.state()
    )
    records = after.tick(AT.replace(minute=31, second=30))
    assert [(r["type"], r["body"].get("how")) for r in records] == [
        ("incident_resolved", "rule_retired")
    ]
    assert "ingest.kafka-decode-errors" not in after.state()["machines"]


def test_a_grouped_rule_with_no_groups_yet_is_not_shown_healthy() -> None:
    path = CONTRACTS / "fixtures/rules/valid/ingest-source-silent.yaml"
    engine = _engine(yaml.safe_load(path.read_text()))
    engine.tick(AT)
    [s] = [s for s in engine.status() if s["rule"] == "ingest.source-silent"]
    assert (s["eval"], s["not_evaluated"]) == ("unknown", "no_groups")


def test_records_are_identical_across_hash_seeds() -> None:
    """Set and dict ordering can't leak into the records (A3-5 determinism)."""
    code = (
        "import hashlib\n"
        "from services.medic.contracts.decision_chain import canonical\n"
        "from services.medic.contracts.vector_expand import load\n"
        "from services.medic.tests.engine.runner import VECTORS, run\n"
        "print(hashlib.sha256(canonical([run(load(p)).records for p in VECTORS])).hexdigest())"
    )
    digests = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": str(CONTRACTS.parents[2])},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("1", "2", "3")
    }
    assert len(digests) == 1
