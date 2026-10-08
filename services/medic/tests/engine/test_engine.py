"""Engine behaviour the vectors don't pin on their own."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from services.medic.contracts.vector_expand import load
from services.medic.engine import Engine, load_rule
from services.medic.engine.evaluate import UNKNOWN, k_all, k_any, k_not
from services.medic.tests.engine.runner import CONTRACTS, INSTANCE, group_key, run

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


def test_observations_from_after_the_tick_are_not_used() -> None:
    vector = load(VECTORS / "v11-latch-reopens.yaml")
    from services.medic.contracts.vector_expand import expand

    engine = Engine(
        [
            load_rule(
                yaml.safe_load(
                    (
                        CONTRACTS / "fixtures/rules/valid/llm-gateway-outage.yaml"
                    ).read_text()
                )
            )
        ],
        instance_id=INSTANCE,
        install_shape="compose",
    )
    for obs in expand(vector):  # all 25 minutes up front
        engine.observe(obs)
    engine.tick(AT.replace(minute=1))
    status = engine.status()
    assert status == [] or status[0]["state"] == "inactive"
