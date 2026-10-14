"""The Monitor contract: every answer is validated against core/monitor/models.py.

Demo mode serves the fixtures, which must satisfy the contract; an
implementation that drifts from it fails at the route instead of on screen.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.monitor import fixtures
from core.monitor.models import (
    FleetWallPayload,
    PulseDecisions,
    PulseSummary,
    PulseThroughput,
    PulseWork,
)
from services.api.routers import monitor

pytestmark = pytest.mark.unit

READS = [
    ("/api/fleet-wall", FleetWallPayload),
    ("/api/pulse/summary", PulseSummary),
    ("/api/pulse/throughput", PulseThroughput),
    ("/api/pulse/work", PulseWork),
    ("/api/pulse/decisions", PulseDecisions),
]


def _client(monkeypatch, demo: bool) -> TestClient:
    monkeypatch.setattr(monitor, "is_demo_mode", lambda: demo)
    app = FastAPI()
    app.include_router(monitor.router, prefix=monitor.ROUTER_META.prefix)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("path", "model"), READS)
def test_demo_mode_serves_fixtures_that_satisfy_the_contract(monkeypatch, path, model):
    response = _client(monkeypatch, demo=True).get(path)
    assert response.status_code == 200
    model.model_validate(response.json())


def test_the_fixture_wall_draws_every_state_the_console_filters_by():
    wall = fixtures.wall_payload()
    states = {cell.state for band in wall.bands for cell in band.cells or []}
    assert {"needs_you", "stuck", "running", "paused", "on_hold"} <= states
    assert [f.key for f in wall.facets] == [
        "all",
        "needs_you",
        "stuck",
        "near_limit",
        "running",
        "paused",
        "on_hold",
    ]
    assert wall.facets[0].count == sum(len(b.cells or []) for b in wall.bands)


def test_the_fixture_wall_is_stable_between_polls():
    first = fixtures.wall_payload().model_dump(exclude={"now", "just_closed"})
    second = fixtures.wall_payload().model_dump(exclude={"now", "just_closed"})
    for band in first["bands"] + second["bands"]:
        for cell in band["cells"]:
            cell.pop("decision")
    assert first == second


def test_throughput_kinds_carry_no_cells():
    assert all(kind.cells is None for kind in fixtures.throughput().kinds)


def test_a_read_that_matches_the_contract_is_served(monkeypatch):
    client = _client(monkeypatch, demo=False)
    monkeypatch.setattr(monitor, "work", lambda: fixtures.work().model_dump())
    response = client.get("/api/pulse/work")
    assert response.status_code == 200
    assert PulseWork.model_validate(response.json()).rows


def test_a_read_that_drifts_from_the_contract_is_refused(monkeypatch):
    client = _client(monkeypatch, demo=False)
    drifted = fixtures.wall_payload().model_dump()
    del drifted["slots"]
    monkeypatch.setattr(monitor, "wall_payload", lambda **_: drifted)
    assert client.get("/api/fleet-wall").status_code == 500


def test_an_unknown_cell_state_is_refused(monkeypatch):
    client = _client(monkeypatch, demo=False)
    drifted = fixtures.wall_payload().model_dump()
    drifted["bands"][0]["cells"][0]["state"] = "deferred"
    monkeypatch.setattr(monitor, "wall_payload", lambda **_: drifted)
    assert client.get("/api/fleet-wall").status_code == 500


def test_the_stop_grid_has_one_count_per_reason_and_kind():
    stops = fixtures.decisions().stops
    assert len(stops.counts) == len(stops.reasons)
    assert all(len(row) == len(stops.kinds) for row in stops.counts)


def test_who_decided_covers_seven_days_ending_today():
    payload = fixtures.decisions()
    assert len(payload.who.days) == 7
    assert payload.who.days[-1].day == payload.now[:10]
