"""The Monitor reads answer 501 until implemented; the helpers they will use are sound."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.monitor.common import (
    UNMEASURED,
    metric,
    rate_level,
    same_time_yesterday,
    usage_level,
)
from services.api.routers import monitor

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(monitor, "is_demo_mode", lambda: False)
    app = FastAPI()
    app.include_router(monitor.router, prefix=monitor.ROUTER_META.prefix)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/fleet-wall",
        "/api/pulse/summary",
        "/api/pulse/throughput",
        "/api/pulse/work",
        "/api/pulse/decisions",
    ],
)
def test_unimplemented_reads_answer_501_with_a_fixed_message(client, path):
    response = client.get(path)
    assert response.status_code == 501
    assert "not implemented yet" in response.json()["detail"]


def test_an_unknown_pulse_tab_is_404(client):
    assert client.get("/api/pulse/nope").status_code == 404


def test_the_router_requires_auth():
    assert monitor.ROUTER_META.auth.name == "REQUIRED"


@pytest.mark.parametrize(
    ("used", "limit", "level"),
    [
        (1, 10, "good"),
        (7.5, 10, "fair"),
        (9, 10, "fair"),
        (9.1, 10, "poor"),
        (5, None, None),
        (None, 10, None),
    ],
)
def test_usage_level_bands(used, limit, level):
    assert usage_level(used, limit) == level


@pytest.mark.parametrize(
    ("rate", "level"),
    [(0.95, "good"), (0.9, "fair"), (0.85, "fair"), (0.84, "poor"), (None, None)],
)
def test_rate_level_bands(rate, level):
    assert rate_level(rate) == level


def test_an_unmeasured_metric_carries_no_value_or_level():
    body = metric(3, info="how", level="good", unmeasured="Not recorded.")
    assert body["value"] is None and body["level"] is None
    assert (
        body["unmeasured_text"] == UNMEASURED
        and body["unmeasured_reason"] == "Not recorded."
    )


def test_same_time_yesterday_runs_from_midnight():
    start, end = same_time_yesterday(datetime(2026, 10, 14, 14, 20))
    assert start == datetime(2026, 10, 13) and end == datetime(2026, 10, 13, 14, 20)
