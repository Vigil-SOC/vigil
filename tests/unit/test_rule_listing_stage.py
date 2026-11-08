"""The Detections rule listing carries each copy's rollout stage and the days it
has spent in shadow, and narrows to one stage (#3995)."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.detections.inventory import catalog
from services.api.routers import detection_rules as routes

TODAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)


def _copy(rule_id):
    return SimpleNamespace(
        id=rule_id,
        name=f"R{rule_id}",
        language="kql",
        source="elastic",
        status="active",
        techniques=[],
        checks=None,
    )


def _at(day):
    return datetime(2026, 11, day, 23, 0, tzinfo=timezone.utc)


def _built(stages):
    copies = [_copy(i) for i in (1, 2, 3, 4)]
    return catalog._health_rows(
        {
            "rows": copies,
            "rule_of": {c.id: c.id for c in copies},
            "stages": stages,
        },
        {},
        today=TODAY,
    )


STAGES = {1: ("shadow", _at(2)), 2: ("live", _at(1)), 3: ("off", _at(1))}


@pytest.fixture
def listing(monkeypatch):
    monkeypatch.setattr(
        catalog, "_listing_now", lambda: {**_built(STAGES), "blind_spots": []}
    )


def _by_id(out):
    return {r["rule_id"]: r for r in out["rules"]}


def test_rows_carry_stage_and_days_in_shadow(listing):
    rows = _by_id(catalog.health_listing(now=NOW))
    # In shadow since Nov 2 23:00: 4 days 23 hours is 4 whole days.
    assert (rows[1]["stage"], rows[1]["days_in_shadow"]) == ("shadow", 4)
    assert (rows[2]["stage"], rows[2]["days_in_shadow"]) == ("live", None)
    assert (rows[3]["stage"], rows[3]["days_in_shadow"]) == ("off", None)
    # No stage row (a copy outside the overlay): null stage.
    assert (rows[4]["stage"], rows[4]["days_in_shadow"]) == (None, None)


@pytest.mark.parametrize("stage, ids", [("shadow", [1]), ("live", [2]), ("off", [3])])
def test_stage_filter_keeps_one_stage(listing, stage, ids):
    out = catalog.health_listing(stage=stage)
    assert [r["rule_id"] for r in out["rules"]] == ids
    assert out["matched"] == len(ids)


def test_unknown_stage_matches_nothing(listing):
    assert catalog.health_listing(stage="bogus")["rules"] == []


def test_stage_filter_combines_with_the_others(listing):
    assert catalog.health_listing(stage="live", tool="splunk")["rules"] == []
    assert len(catalog.health_listing(stage="live", tool="elastic")["rules"]) == 1


def _client():
    # Only get_rules: the router's permission gate is not what is under test.
    gateless = APIRouter()
    gateless.add_api_route("/rules", routes.get_rules, methods=["GET"])
    app = FastAPI()
    app.include_router(gateless)
    return TestClient(app)


def test_route_passes_stage(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        catalog, "health_listing", lambda *a, **kw: seen.update(kw) or {}
    )
    routes.get_rules(
        health=None,
        tool=None,
        q=None,
        offset=0,
        sort="name",
        rules=None,
        stage="shadow",
    )
    assert seen["stage"] == "shadow"


def test_route_rejects_an_unknown_stage(monkeypatch):
    monkeypatch.setattr(catalog, "health_listing", lambda *a, **kw: {})
    client = _client()
    assert client.get("/rules?stage=bogus").status_code == 422
    assert client.get("/rules?stage=live").status_code == 200


@pytest.mark.parametrize(
    "now, days",
    [
        (datetime(2026, 11, 7, 22, 59, tzinfo=timezone.utc), 4),
        (datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc), 5),
        (datetime(2026, 11, 3, 22, 59, tzinfo=timezone.utc), 0),
    ],
)
def test_days_in_shadow_are_whole_24_hour_days_like_rollout(listing, now, days):
    """Rollout promotes once now - stage_at reaches the shadow days, so the
    listing counts whole 24-hour days from stage_at, not calendar days."""
    rows = _by_id(catalog.health_listing(now=now))
    assert rows[1]["days_in_shadow"] == days


def test_current_stage_orders_by_time_then_id_like_rollout():
    """A stage row written later in time wins even when its id is lower, as in
    rollout._copies: newest at first, id only breaks a tie."""
    from sqlalchemy.dialects import postgresql

    sql = str(catalog.CURRENT_STAGE.compile(dialect=postgresql.dialect()))
    order = sql.split("ORDER BY", 1)[1]
    assert order.index("rule_id") < order.index("at DESC") < order.index("id DESC")


def test_rows_carry_stage_since(listing):
    """When the current stage row was written, so a row can read "Live since 2 Nov"."""
    rows = _by_id(catalog.health_listing(now=NOW))
    assert rows[1]["stage_since"] == _at(2).isoformat()
    assert rows[2]["stage_since"] == _at(1).isoformat()
    assert rows[4]["stage_since"] is None


def test_stage_counts_are_counted_before_the_stage_filter(listing):
    """Like flag_counts: every listed copy's stage, whatever stage is shown."""
    for stage in (None, "shadow", "live"):
        out = catalog.health_listing(stage=stage, now=NOW)
        assert out["stage_counts"] == {"shadow": 1, "live": 1, "off": 1}
