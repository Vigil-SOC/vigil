"""`GET /v1/sensors`: the scheduler's per-sensor view, published with the status."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from services.medic.api.sensors import build_sensors
from services.medic.api.server import SNAPSHOT_MAX_AGE_S, StatusBoard, make_server
from services.medic.sensors import TICK_S, ReadError
from services.medic.sensors.scheduler import _reason
from services.medic.tests.api.schema import errors
from services.medic.tests.api.test_server import KEY, _snapshot, assert_problem, call
from services.medic.tests.fakes import FakeClock
from services.medic.tests.sensors.harness import FakeSensor, Rig

BLIND_ROW = {"rule": "watcher.sensor-blind", "group": {"sensor": "bad.one"}}
BLIND_ROW |= {"state": "firing", "incident_id": "inc_0000000000abcdef"}


def test_states_reasons_and_since_from_the_scheduler() -> None:
    asyncio.run(_states_reasons_and_since())


async def _states_reasons_and_since() -> None:
    rig = Rig(
        [
            FakeSensor("bad.one", mode="error"),
            FakeSensor("good.one", covers=("api_health_ready",)),
            FakeSensor("api.one", uses_vigil_api=True),
        ],
        dev_mode=True,
    )
    await rig.tick()
    first = {s["id"]: s for s in rig.scheduler.sensor_states()}
    assert first["bad.one"]["state"] == "reporting"  # one failure isn't blind yet
    assert first["api.one"] | {"since": None} == {
        "id": "api.one",
        "state": "off",
        "since": None,
        "last_good_at": None,
        "reason": "disabled_by_config",
    }
    for _ in range(8):
        await rig.tick(TICK_S)
    now = {s["id"]: s for s in rig.scheduler.sensor_states()}
    assert (now["bad.one"]["state"], now["bad.one"]["reason"]) == (
        "cant_see",
        "unreachable",
    )
    assert now["bad.one"]["since"] != first["bad.one"]["since"]
    assert now["good.one"]["since"] == first["good.one"]["since"]
    assert now["good.one"]["last_good_at"] is not None

    doc = build_sensors(list(now.values()), [BLIND_ROW], now=rig.clock.wall())
    assert errors("SensorList", doc) == []
    bad = next(s for s in doc["items"] if s["id"] == "bad.one")
    assert bad["watcher_incident_id"] == "inc_0000000000abcdef"
    assert bad["dropped_last_hour"] is None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ReadError("dns"), "unreachable"),
        (ReadError("auth", http_status=401), "auth_401"),
        (ReadError("http_status", http_status=403), "auth_403"),
        (ReadError("http_status", http_status=503), "http_5xx"),
        (ReadError("http_status", http_status=404), "not_found"),
        (ReadError("timeout"), "timeout"),
        (ReadError("parse"), "parse_error"),
        (ReadError("tls"), "unknown"),
        (None, "unknown"),
    ],
)
def test_read_error_reasons(error, reason) -> None:
    assert _reason(error) == reason


def test_an_id_outside_the_contract_is_not_served() -> None:
    row = {"state": "reporting", "since": None, "last_good_at": None, "reason": None}
    doc = build_sensors(
        [{"id": "docker.logs.soc-daemon", **row}, {"id": "kube.pods", **row}],
        [],
        now=0,
    )
    assert [s["id"] for s in doc["items"]] == ["kube.pods"]


def test_the_listener_serves_the_published_list_until_it_goes_stale(tmp_path) -> None:
    key = tmp_path / "api_key"
    key.write_text(KEY + "\n")
    clock, board = FakeClock(), StatusBoard()
    srv = make_server(("127.0.0.1", 0), key_file=key, board=board, wall=clock.wall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        h = {"X-Medic-Key": KEY}
        assert_problem(*call(srv, target="/v1/sensors", headers=h), 503, "busy")
        now = clock.wall()
        doc = build_sensors([], [], now=now)
        board.publish(_snapshot(now), started_at=now - 60, taken_at=now, sensors=doc)
        status, _, body = call(srv, target="/v1/sensors", headers=h)
        assert status == 200 and json.loads(body) == doc
        assert errors("SensorList", json.loads(body)) == []
        # The status body is unchanged by the list riding along.
        assert "items" not in json.loads(call(srv, headers=h)[2])
        assert_problem(
            *call(srv, target="/v1/sensors?x=1", headers=h), 404, "not_found"
        )
        assert_problem(*call(srv, target="/v1/sensors"), 401, "unauthorized")
        clock.advance(SNAPSHOT_MAX_AGE_S + 1)
        assert_problem(*call(srv, target="/v1/sensors", headers=h), 503, "busy")
    finally:
        srv.shutdown()
        srv.server_close()
