"""K2 canary: one fake secret per redaction family, planted in every free-text
place a sensor can put one, is absent from the sink and from Medic's own log."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from services.medic.redact import Redactor, install_log_redaction
from services.medic.sensors import ReadError, Reading, Value
from services.medic.tests.sensors.harness import FakeSensor, Rig

MEDIC = Path(__file__).resolve().parents[2]
CASES = json.loads((MEDIC / "contracts/fixtures/redaction/cases.json").read_text())[
    "must_redact"
]


def _planted(case: dict) -> list[Reading]:
    line = case["input"]
    return [
        Reading("api_health", "backend", values=[Value.text("detail", line)]),
        Reading(
            "api_health",
            "backend",
            values=[Value.flag("ok", False, labels={"source": line})],
        ),
        Reading("api_health", "backend", values=[Value.enum("verdict", line)]),
        Reading("api_health", "backend", error=ReadError("other", detail=line)),
        Reading(
            "api_health", "backend", instance=line, values=[Value.flag("ok", True)]
        ),
    ]


async def test_canary_per_family_absent_from_sink_and_log() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger("services.medic")
    root.addHandler(handler)
    handle = install_log_redaction()
    try:
        readings = [r for case in CASES for r in _planted(case)]
        rig = Rig(choke=None, sensors=[FakeSensor("canary.one", readings=readings)])
        await rig.tick()
        for case in CASES:  # Medic logging what it read, the worst case
            logging.getLogger("services.medic.sensors").warning(
                "read %s", case["input"]
            )
    finally:
        handle.uninstall()
        root.removeHandler(handler)
    sink = json.dumps(rig.sink.observations)
    log = stream.getvalue()
    for case in CASES:
        assert case["secret"] not in sink, case["id"]
        assert case["secret"] not in log, case["id"]
    samples = rig.sink.of("sample")
    assert len(samples) == 5 * len(CASES)
    # Every planted variant must register a hit: label_safe would hide a miss in
    # a label, enum or instance from the "not in sink" check (review S4).
    missed = [s for s in samples if s["redaction"]["hits"] < 1]
    assert missed == []
    rig.assert_all_valid()


async def test_redactor_exception_drops_the_payload_and_counts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(
        choke=None,
        sensors=[
            FakeSensor(
                "canary.one",
                readings=[
                    Reading("api_health", "backend", values=[Value.text("d", "x")])
                ],
            )
        ],
    )
    calls = {"n": 0}
    real = Redactor.redact

    def flaky(self: Redactor, text: str) -> tuple[str, int]:
        calls["n"] += 1
        if text == "x":
            raise RuntimeError("redactor bug")
        return real(self, text)

    monkeypatch.setattr(Redactor, "redact", flaky)
    out = await rig.tick()
    assert [o["kind"] for o in out] == [
        "sensor_health"
    ]  # the sample never reached the sink
    assert rig.stats["canary.one"].redactor_failures == 1
    assert rig.scheduler.status()["redactor_failures"] == 1
    await rig.tick(30)
    health = rig.health("canary.one")[-1]
    assert health["counters"]["redactor_failures"] == 1


async def test_text_is_capped_after_redaction_not_before() -> None:
    # A secret straddling the 500-char cap must not survive as a fragment.
    secret = "sk-ant-" + "canary0031" * 5
    line = "x" * 480 + " " + secret
    rig = Rig(
        choke=None,
        sensors=[
            FakeSensor(
                "cap.one",
                readings=[
                    Reading("api_health", "backend", values=[Value.text("d", line)])
                ],
            )
        ],
    )
    await rig.tick()
    value = rig.sink.of("sample")[0]["values"][0]["value"]
    assert "canary0031" not in value and value.endswith("[REDACTED]")


async def test_labels_are_redacted_before_they_are_hashed() -> None:
    from services.medic.contracts.fingerprint_ref import label_safe

    raw = "password=hunter2hunter2"
    reading = Reading(
        "api_health",
        "backend",
        instance="agent-worker:6990",
        values=[Value.flag("ok", True, {"src": raw})],
    )
    rig = Rig(choke=None, sensors=[FakeSensor("label.one", readings=[reading])])
    await rig.tick()
    sample = rig.sink.of("sample")[0]
    assert sample["values"][0]["labels"]["src"] == label_safe("password=[REDACTED]")
    assert sample["values"][0]["labels"]["src"] != label_safe(raw)
    assert sample["target"]["instance"] == "agent-worker:6990"  # fits the pattern: kept


def test_production_default_is_the_redaction_choke() -> None:
    from services.medic.sensors import Bus, MemorySink, Pipeline, Stats

    pipeline = Pipeline(Bus(Stats()), MemorySink())
    draft = {
        "kind": "sample",
        "values": [],
        "target": {"service": "backend", "shape": "helm"},
    }
    assert pipeline.choke(draft)["redaction"]["version"] == "k2min-1"


async def test_userinfo_in_a_label_shaped_value_is_redacted() -> None:
    # Review S1: "user:pw@host" fits the label pattern, so label_safe keeps it.
    reading = Reading(
        "api_health",
        "backend",
        instance="vigil:canaryUserinfo0032@agent-worker:6990",
        values=[Value.flag("ok", True, {"via": ":canaryUserinfo0033@redis:6379"})],
    )
    rig = Rig(choke=None, sensors=[FakeSensor("ui.one", readings=[reading])])
    await rig.tick()
    sample = rig.sink.of("sample")[0]
    assert "canaryUserinfo" not in json.dumps(rig.sink.observations)
    assert sample["redaction"]["hits"] == 2
