"""triage arrivals and probe outcomes through the gateway (D6-4)."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from services.medic.sensors.gateway import assert_path_subset
from services.medic.sensors.work import (
    PROBE_CALL,
    TRIAGE_CALL,
    ProbeFindings,
    TriageArrivals,
)
from services.medic.tests.sensors.harness import Rig

CANARY = "canaryWork0070"
GW = ("medic-gateway-out", 8471)


def _reply(status: int, body: Any, content_type: str = "application/json"):
    raw = json.dumps(body).encode()
    return httpx.Response(status, content=raw, headers={"content-type": content_type})


class _Gateway:
    """A MockTransport that answers by path and records what was asked."""

    def __init__(self, **by_path: httpx.Response) -> None:
        self.by_path = by_path
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        self.requests.append(request)
        return self.by_path[request.url.path]


def _triage(gw: _Gateway, **kw: Any) -> TriageArrivals:
    return TriageArrivals(*GW, transport=gw.transport, **kw)


def _probes(gw: _Gateway) -> ProbeFindings:
    return ProbeFindings(*GW, transport=gw.transport)


async def _cycle(rig: Rig, advance: float = 0) -> list[dict]:
    before = len(rig.sink.observations)
    rig.clock.advance(advance)
    await rig.scheduler.tick()
    for _ in range(200):
        if all(s.task is None or s.task.done() for s in rig.scheduler._states.values()):
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0)
    rig.pipeline.drain()
    return rig.sink.observations[before:]


def _samples(observations: list[dict], signal: str) -> list[dict]:
    return [o for o in observations if o["kind"] == "sample" and o["signal"] == signal]


def _values(obs: dict) -> list[tuple]:
    return [
        (
            (v["key"], v["value"], v.get("labels", {}).get("source"))
            if v["key"] == "sources.arrivals"
            else (v["key"], v["value"], v.get("labels", {}).get("probe"))
        )
        for v in obs["values"]
    ]


def _finding(name: str, outcome: str | None, day: str = "2026-10-28") -> dict:
    probe: dict[str, Any] = {"name": name, "expected": {"severity": ["low"]}}
    if outcome is not None:
        probe["score"] = {"outcome": outcome, "verdict": {"severity": CANARY}}
    return {
        "finding_id": f"probe:{name}:{day}",
        "data_source": "probe",
        "description": f"password={CANARY}",
        "entity_context": {"hostnames": [CANARY], "probe": probe},
        "ai_enrichment": {"ai_triage": {"result": {"reasoning": CANARY}}},
    }


TRIAGE_BODY = {
    "rows": [{"title": CANARY, "summary": f"token={CANARY}"}],
    "strip": {"queued": 3},
    "sources": [
        {"data_source": "splunk", "arrivals": 12, "lag_seconds": 4, "quiet": False},
        {"data_source": "crowdstrike", "arrivals": 0, "lag_seconds": None},
    ],
}


def test_declared_calls_are_on_the_gateway_list() -> None:
    assert_path_subset((TRIAGE_CALL, PROBE_CALL))


async def test_each_request_sends_exactly_its_declared_query() -> None:
    gw = _Gateway(
        **{
            "/api/triage": _reply(200, TRIAGE_BODY),
            "/api/v1/findings": _reply(200, {"findings": []}),
        }
    )
    rig = Rig([_triage(gw), _probes(gw)])
    await _cycle(rig)
    sent = {r.url.path: tuple(sorted(r.url.params.keys())) for r in gw.requests}
    assert sent == {
        TRIAGE_CALL.path: tuple(sorted(TRIAGE_CALL.query)),
        PROBE_CALL.path: tuple(sorted(PROBE_CALL.query)),
    }
    (findings,) = [r for r in gw.requests if r.url.path == PROBE_CALL.path]
    assert findings.url.params["data_source"] == "probe"
    assert 1 <= int(findings.url.params["limit"]) <= 50


async def test_arrivals_are_counters_per_source_and_bodies_are_not_stored() -> None:
    findings = {
        "findings": [
            _finding("c2-beacon-exfil", "miss"),
            _finding("c2-beacon-exfil", "hit", day="2026-10-27"),  # older
            _finding("routine-patch-window", None),  # under an hour: no score
            _finding("phish-click", "silent")
            | {"entity_context": {"probe": {"score": {"outcome": "silent"}}}},
        ]
    }
    gw = _Gateway(
        **{
            "/api/triage": _reply(200, TRIAGE_BODY),
            "/api/v1/findings": _reply(200, findings),
        }
    )
    rig = Rig([_triage(gw), _probes(gw)], choke=None)
    out = await _cycle(rig)
    rig.assert_all_valid()
    (triage,) = _samples(out, "triage")
    assert triage["outcome"] == "ok" and triage["target"]["service"] == "backend"
    assert _values(triage) == [
        ("ok", True, None),
        ("sources.arrivals", 12, "splunk"),
        ("sources.arrivals", 0, "crowdstrike"),
    ]
    assert triage["epoch"].startswith("utc-day:")
    (probe,) = _samples(out, "probe_findings")
    # Name from entity_context.probe.name, else from the finding_id.
    assert _values(probe) == [
        ("ok", True, None),
        ("latest_outcome", "miss", "c2-beacon-exfil"),
        ("latest_outcome", "silent", "phish-click"),
    ]
    assert CANARY not in json.dumps(rig.sink.observations)


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/problem+json"]
)
async def test_a_relayed_500_on_each_route_is_a_result(content_type: str) -> None:
    body = {"status": 500, "detail": CANARY}
    gw = _Gateway(
        **{
            "/api/triage": _reply(500, body, content_type),
            "/api/v1/findings": _reply(500, body, content_type),
        }
    )
    rig = Rig([_triage(gw), _probes(gw)])
    out = await _cycle(rig)
    rig.assert_all_valid()
    for signal in ("triage", "probe_findings"):
        (obs,) = _samples(out, signal)
        assert obs["outcome"] == "ok"
        assert _values(obs) == [("ok", False, None), ("status_code", 500, None)]
    assert CANARY not in json.dumps(rig.sink.observations)


async def test_an_empty_list_is_ok_with_nothing_in_it() -> None:
    gw = _Gateway(
        **{
            "/api/triage": _reply(200, {"rows": [], "sources": []}),
            "/api/v1/findings": _reply(200, {"findings": [], "total": 0}),
        }
    )
    rig = Rig([_triage(gw), _probes(gw)])
    out = await _cycle(rig)
    rig.assert_all_valid()
    for signal in ("triage", "probe_findings"):
        (obs,) = _samples(out, signal)
        assert obs["outcome"] == "ok"
        assert _values(obs) == [("ok", True, None)]


@pytest.mark.parametrize(
    "outcome", ["pending", "HIT", None, 1], ids=["pending", "upper", "null", "int"]
)
async def test_an_outcome_outside_the_set_is_a_parse_error(outcome: Any) -> None:
    finding = _finding("c2-beacon-exfil", "hit")
    finding["entity_context"]["probe"]["score"]["outcome"] = outcome
    gw = _Gateway(**{"/api/v1/findings": _reply(200, {"findings": [finding]})})
    rig = Rig([_probes(gw)])
    (obs,) = _samples(await _cycle(rig), "probe_findings")
    assert obs["outcome"] == "error" and obs["error"]["class"] == "parse"


@pytest.mark.parametrize(
    "sources",
    [None, [{"data_source": "x", "arrivals": -1}], [{"data_source": "x"}], ["x"]],
)
async def test_a_triage_body_it_cannot_read_is_a_parse_error(sources: Any) -> None:
    gw = _Gateway(**{"/api/triage": _reply(200, {"sources": sources})})
    rig = Rig([_triage(gw)])
    (obs,) = _samples(await _cycle(rig), "triage")
    assert obs["outcome"] == "error" and obs["error"]["class"] == "parse"


async def test_the_gateways_own_failure_is_an_error() -> None:
    body = {"status": 502, "code": "auth_bad_password"}
    gw = _Gateway(**{"/api/triage": _reply(502, body, "application/problem+json")})
    rig = Rig([_triage(gw)])
    (obs,) = _samples(await _cycle(rig), "triage")
    assert obs["outcome"] == "error"
    assert obs["error"] == {
        "class": "auth",
        "http_status": 502,
        "detail": "auth_bad_password",
    }


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("make", [TriageArrivals, ProbeFindings])
async def test_unreachable_gateway_is_an_error_then_blind(make: type) -> None:
    sensor = make("127.0.0.1", _closed_port())
    rig = Rig([sensor])
    first = await _cycle(rig)
    (obs,) = [o for o in first if o["kind"] == "sample"]
    assert obs["outcome"] == "error" and obs["error"]["class"] == "refused"
    await _cycle(rig, sensor.interval_s)
    await _cycle(rig, sensor.interval_s)
    assert rig.health(sensor.id)[-1]["state"] == "blind"
    rig.assert_all_valid()


async def test_the_arrivals_epoch_changes_at_utc_midnight() -> None:
    gw = _Gateway(**{"/api/triage": _reply(200, TRIAGE_BODY)})
    # Late-bound: reads the clock of the rig built on the next line.
    sensor = _triage(
        gw, today=lambda: datetime.fromtimestamp(rig.clock.wall(), UTC).date()
    )
    rig = Rig([sensor])
    # The FakeClock starts on 15 Jan 2027: 30 s before the next UTC midnight.
    midnight = datetime(2027, 1, 16, tzinfo=UTC).timestamp()
    rig.clock.advance(midnight - 30 - rig.clock.wall())
    before = _samples(await _cycle(rig), "triage")
    after = _samples(await _cycle(rig, sensor.interval_s), "triage")
    assert [o["epoch"] for o in before + after] == [
        "utc-day:2027-01-15",
        "utc-day:2027-01-16",
    ]


async def test_dev_mode_turns_both_off() -> None:
    gw = _Gateway()
    rig = Rig([_triage(gw), _probes(gw)], dev_mode=True)
    await _cycle(rig)
    assert set(rig.scheduler.status()["off"]) == {
        "gateway.triage",
        "gateway.probe_findings",
    }
    assert gw.requests == []
