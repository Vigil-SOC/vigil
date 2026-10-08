"""D5: a dead sensor is never silence; drops are counted; everything validates."""

from __future__ import annotations

from itertools import pairwise

import pytest

from services.medic.sensors import FRAMEWORK_ID, TICK_S, Reading, Value
from services.medic.tests.sensors.harness import FakeSensor, Rig, stamp_only


async def test_ok_sensor_writes_sample_and_health_every_interval() -> None:
    rig = Rig([FakeSensor("ok.one")])
    first = await rig.tick()
    assert [o["kind"] for o in first] == ["sample", "sensor_health"]
    assert first[1]["health"]["state"] == "ok"
    assert first[1]["health"]["reported_by"] == "sensor"
    assert await rig.tick(TICK_S) == []  # not due yet: fixed 30 s interval
    assert len(await rig.tick(TICK_S)) == 2
    rig.assert_all_valid()


@pytest.mark.parametrize("mode", ["crash", "hang"])
async def test_crash_or_hang_is_reported_within_one_tick(mode: str) -> None:
    bad, good = (
        FakeSensor("bad.one", mode=mode),
        FakeSensor("good.one", covers=("api_health_ready",)),
    )
    rig = Rig([bad, good])
    out = await rig.tick()
    out += await rig.tick(TICK_S)  # a hang is cut on the next tick (timeout 5 s < 15 s)
    errors = [o for o in out if o["kind"] == "sample" and o["outcome"] == "error"]
    assert [(o["signal"], o["error"]["class"]) for o in errors] == [
        ("api_health", "other" if mode == "crash" else "timeout")
    ]
    health = rig.health("bad.one")[-1]
    assert health["reported_by"] == "framework"
    assert health["state"] == "degraded"
    assert health["consecutive_failures"] == 1
    assert health["last_error"]["class"] == errors[0]["error"]["class"]
    assert [h["state"] for h in rig.health("good.one")] == ["ok"]
    rig.assert_all_valid()


async def test_crash_detail_is_the_type_only() -> None:
    rig = Rig([FakeSensor("bad.one", mode="crash")])
    await rig.tick()
    text = repr(rig.sink.observations)
    assert "canaryCrash0030" not in text
    assert "RuntimeError" in text


@pytest.mark.parametrize("mode", ["crash", "hang"])
async def test_three_failed_cycles_read_blind_and_others_keep_schedule(
    mode: str,
) -> None:
    bad, good = (
        FakeSensor("bad.one", mode=mode),
        FakeSensor("good.one", covers=("api_health_ready",)),
    )
    rig = Rig([bad, good])
    for _ in range(8):  # 2 minutes
        await rig.tick(TICK_S)
    states = [h["state"] for h in rig.health("bad.one")]
    assert "blind" in states
    assert states.index("blind") >= 2  # never before the third failure
    assert good.calls == 4  # every 30 s, untouched by the bad one
    rig.assert_all_valid()


async def test_sensor_that_ignores_cancellation_reads_stopped() -> None:
    rig = Rig([FakeSensor("stuck.one", mode="stubborn")])
    for _ in range(8):
        await rig.tick(TICK_S)
    last = rig.health("stuck.one")[-1]
    assert last["state"] == "stopped"
    assert last["reported_by"] == "framework"
    stuck = rig.scheduler._states["stuck.one"]
    assert stuck.sensor.calls == 1  # never started twice
    rig.assert_all_valid()


async def test_backoff_only_after_three_errors_and_resets_on_success() -> None:
    sensor = FakeSensor("err.one", mode="error")
    rig = Rig([sensor])
    for _ in range(20):  # 5 minutes
        await rig.tick(TICK_S)
    # 30 s for the first three, then 60 s, then 120 s (4 × 30, capped)
    assert 4 <= sensor.calls <= 6
    assert rig.health("err.one")[-1]["state"] == "blind"
    sensor.mode = "ok"
    for _ in range(12):
        await rig.tick(TICK_S)
    assert rig.health("err.one")[-1]["state"] == "ok"
    before = sensor.calls
    for _ in range(4):
        await rig.tick(TICK_S)
    assert sensor.calls == before + 2  # back to every 30 s


async def test_health_keeps_coming_while_backing_off() -> None:
    rig = Rig([FakeSensor("err.one", mode="error")])
    times = []
    for i in range(24):
        if any(o["kind"] == "sensor_health" for o in await rig.tick(TICK_S)):
            times.append(i * TICK_S)
    gaps = {b - a for a, b in pairwise(times)}
    assert max(gaps) <= 30


async def test_completed_read_saying_vigil_is_down_never_backs_off() -> None:
    down = Reading("api_health", "backend", values=[Value.flag("ok", False)])
    sensor = FakeSensor("down.one", readings=[down])
    rig = Rig([sensor])
    for _ in range(20):
        await rig.tick(TICK_S)
    assert sensor.calls == 10
    assert {h["state"] for h in rig.health("down.one")} == {"ok"}


async def test_queue_overflow_is_counted_not_silent() -> None:
    many = [
        Reading("api_health", "backend", values=[Value.gauge("n", i)])
        for i in range(10)
    ]
    rig = Rig([FakeSensor("flood.one", readings=many)], capacity=4)
    out = await rig.tick()
    samples = [o for o in out if o["kind"] == "sample"]
    assert [s["values"][0]["value"] for s in samples] == [6, 7, 8, 9]  # oldest dropped
    health = rig.health("flood.one")[-1]
    assert health["counters"]["dropped"] == 6
    assert health["state"] == "degraded"
    assert rig.scheduler.status()["dropped"] == 6
    rig.assert_all_valid()


async def test_seq_has_no_gaps_per_emitter() -> None:
    rig = Rig(
        [
            FakeSensor("a.one"),
            FakeSensor("b.one", mode="crash", covers=("api_health_ready",)),
        ]
    )
    for _ in range(6):
        await rig.tick(TICK_S)
    by_emitter: dict[str, list[int]] = {}
    for o in rig.sink.observations:
        by_emitter.setdefault(o["sensor"]["id"], []).append(o["sensor"]["seq"])
    assert set(by_emitter) == {"a.one", "b.one", FRAMEWORK_ID}
    for seqs in by_emitter.values():
        assert seqs == list(range(len(seqs)))


async def test_invalid_reading_is_a_counted_failure() -> None:
    wrong = [Reading("bifrost_health", "backend", values=[Value.flag("ok", True)])]
    rig = Rig([FakeSensor("wrong.one", readings=wrong)])
    out = await rig.tick()
    assert [o["error"]["class"] for o in out if o["outcome"] == "error"] == ["other"]
    rig.assert_all_valid()


async def test_dev_mode_turns_off_api_sensors_only() -> None:
    api, probe = (
        FakeSensor("api.one", uses_vigil_api=True),
        FakeSensor("probe.one", covers=("api_health_ready",)),
    )
    rig = Rig([api, probe], dev_mode=True)
    await rig.tick()
    assert api.calls == 0 and probe.calls == 1
    status = rig.scheduler.status()
    assert status["off"] == {"api.one": "vigil_dev_mode"}
    assert status["vigil_dev_mode"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_s", 45),
        ("timeout_s", 30),
        ("covers", ("no_such_signal",)),
        ("id", "Bad Id"),
    ],
)
def test_contract_breaking_sensor_is_refused_at_registration(
    field: str, value: object
) -> None:
    sensor = FakeSensor("x.one")
    setattr(sensor, field, value)
    with pytest.raises(ValueError):
        Rig([sensor])


async def test_choke_failure_drops_the_payload_and_counts_it() -> None:
    def broken(draft: dict) -> dict:
        if draft["kind"] == "sample":
            raise RuntimeError("redaction bug")
        return stamp_only(draft)

    rig = Rig([FakeSensor("ok.one")], choke=broken)
    out = await rig.tick()
    assert [o["kind"] for o in out] == ["sensor_health"]
    assert rig.stats["ok.one"].redactor_failures == 1
    await rig.tick(30)
    assert rig.health("ok.one")[-1]["counters"]["redactor_failures"] == 1
    assert rig.health("ok.one")[-1]["state"] == "degraded"
    assert rig.scheduler.status()["redactor_failures"] == 2
