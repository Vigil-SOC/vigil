"""federation_sources (D6-3), read through the gateway."""

from __future__ import annotations

import json
from typing import Any

import httpx

from services.medic.sensors.base import SensorContext
from services.medic.sensors.federation import CALLS, EPOCH, FederationSources
from services.medic.sensors.gateway import assert_path_subset
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="compose")
CANARY = "canaryFedErr0069 password=hunter2fed"
KEYS = {
    "sources.enabled",
    "sources.last_success_at",
    "sources.consecutive_errors",
    "sources.dropped_total",
    "sources.is_configured",
    "global.enabled",
}


def _row(source_id: str, **over: Any) -> dict[str, Any]:
    row = {
        "source_id": source_id,
        "enabled": True,
        "interval_seconds": 300,
        "max_items": 100,
        "min_severity": "high",
        "cursor": {"since": "x"},
        "last_poll_at": "2026-10-07T09:59:58+00:00",
        "last_success_at": "2026-10-07T09:59:58.120000+00:00",
        "last_error": None,
        "consecutive_errors": 0,
        "dropped_total": 3,
        "created_at": "2026-10-01T08:00:00+00:00",
        "updated_at": "2026-10-07T09:59:58+00:00",
        "lag_seconds": 2,
        "quiet": False,
        "is_configured": True,
        "default_interval_seconds": 300,
    }
    return row | over


def _body(*rows: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    return {"sources": list(rows), "global": {"enabled": enabled}}


class Backend:
    """Stands in for the gateway; `reply` can change between ticks."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status, self.body, self.paths = status, body, []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            return httpx.Response(
                self.status,
                content=json.dumps(self.body).encode(),
                headers={"content-type": "application/json"},
            )

        return httpx.MockTransport(handler)


def _sensor(backend: Backend) -> FederationSources:
    return FederationSources("medic-gateway-out", 8471, transport=backend.transport())


def _values(obs: list[dict[str, Any]]) -> dict[tuple[str, str | None], Any]:
    out = {}
    for o in obs:
        if o["kind"] == "sample" and o["signal"] == "federation_sources":
            for v in o["values"]:
                out[(v["key"], v.get("labels", {}).get("source"))] = v
    return out


def test_the_one_call_is_on_outbound() -> None:
    assert_path_subset(CALLS)


async def test_emits_exactly_the_rule_keys_per_source() -> None:
    backend = Backend(body=_body(_row("splunk"), _row("crowdstrike")))
    rig = Rig([_sensor(backend)])
    obs = await rig.tick()
    rig.assert_all_valid()
    assert backend.paths == ["/api/federation/sources"]
    got = _values(obs)
    assert {k for k, _ in got} == KEYS
    assert got[("sources.last_success_at", "splunk")]["value"] == (
        "2026-10-07T09:59:58.12Z"
    )
    assert got[("sources.dropped_total", "splunk")]["value"] == 3
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    # A persisted counter: one epoch, whatever the row's age.
    assert sample["outcome"] == "ok" and sample["epoch"] == EPOCH


async def test_disabling_one_source_changes_only_its_flag() -> None:
    backend = Backend(body=_body(_row("splunk"), _row("crowdstrike")))
    rig = Rig([_sensor(backend)])
    before = _values(await rig.tick())
    backend.body = _body(_row("splunk", enabled=False), _row("crowdstrike"))
    after = _values(await rig.tick(60))
    rig.assert_all_valid()
    assert before[("sources.enabled", "splunk")]["value"] is True
    assert after[("sources.enabled", "splunk")]["value"] is False
    changed = {k for k in after if after[k]["value"] != before[k]["value"]}
    assert changed == {("sources.enabled", "splunk")}


async def test_global_off_still_emits_every_source() -> None:
    backend = Backend(body=_body(_row("splunk"), enabled=False))
    rig = Rig([_sensor(backend)])
    got = _values(await rig.tick())
    rig.assert_all_valid()
    assert got[("global.enabled", None)]["value"] is False
    assert got[("sources.enabled", "splunk")]["value"] is True


async def test_an_unseeded_synthetic_row_is_just_a_row() -> None:
    synthetic = {
        "source_id": "sentinel",
        "enabled": False,
        "interval_seconds": 300,
        "max_items": 100,
        "min_severity": None,
        "cursor": {},
        "last_poll_at": None,
        "last_success_at": None,
        "last_error": None,
        "consecutive_errors": 0,
        "dropped_total": 0,
        "lag_seconds": None,
        "quiet": True,
        "is_configured": False,
        "default_interval_seconds": 300,
    }
    rig = Rig([_sensor(Backend(body=_body(synthetic)))])
    obs = await rig.tick()
    rig.assert_all_valid()
    got = _values(obs)
    assert {k for k, _ in got} == KEYS
    assert got[("sources.enabled", "sentinel")]["value"] is False
    assert got[("sources.last_success_at", "sentinel")]["state"] == "absent"
    assert got[("sources.is_configured", "sentinel")]["value"] is False


async def test_a_relayed_503_is_a_result_with_global_absent() -> None:
    backend = Backend(503, {"detail": "Could not read federation sources"})
    rig = Rig([_sensor(backend)])
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    assert sample["outcome"] == "ok"
    assert sample["values"] == [
        {
            "key": "global.enabled",
            "type": "bool",
            "state": "absent",
            "value": None,
            "trust": "trusted",
        }
    ]


async def test_a_200_of_another_shape_is_a_parse_error() -> None:
    for body in ([], {"sources": {}}, {"sources": [{"enabled": True}], "global": {}}):
        (reading,) = await _sensor(Backend(body=body)).collect(CTX)
        assert reading.error is not None and reading.error.cls == "parse"
        assert reading.values == ()


async def test_an_unreachable_gateway_errors_then_reads_blind() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused") from ConnectionRefusedError()

    transport = httpx.MockTransport(refuse)
    rig = Rig([FederationSources("medic-gateway-out", 8471, transport=transport)])
    obs = await rig.tick()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    assert sample["outcome"] == "error" and sample["error"]["class"] == "refused"
    for _ in range(4):
        await rig.tick(240)
    rig.assert_all_valid()
    assert rig.health("federation_sources")[-1]["state"] == "blind"


async def test_last_error_never_leaves_the_sensor() -> None:
    backend = Backend(
        body=_body(_row("splunk", last_error=CANARY, consecutive_errors=4))
    )
    rig = Rig([_sensor(backend)], choke=None)
    await rig.tick()
    rig.assert_all_valid()
    dumped = json.dumps(rig.sink.observations)
    assert "canaryFedErr0069" not in dumped and "hunter2fed" not in dumped
