"""The Detections rule listing behind GET /api/detection-rules/rules: each
copy's health and reason, the filters and pages read from the built rows, the
cache that rebuilds them only on a write, and the route's parameters."""

from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog


def _copy(rule_id, name, source, status="active", checks=None, techniques=()):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        language="kql",
        source=source,
        status=status,
        techniques=list(techniques),
        checks=checks,
    )


RUNS_FAIL = {
    "runs": {
        "result": "fail",
        "reason": "failed: verification_exception\nline 1:34: Unknown column [x]",
    }
}
SEES_SILENT = {
    "runs": {"result": "pass"},
    "sees": {"result": "fail", "reason": "fail: no events in idx-* in 7d"},
}
SEES_FIELD = {
    "runs": {"result": "pass"},
    "sees": {"result": "fail", "reason": "fail: a.b not in the mapping of idx-*"},
}
NEVER = {
    "runs": {"result": "pass"},
    "silent": {"result": "pass", "days_fired": 0, "reason": "no alerts in 30 d"},
}
ALERTED = {"runs": {"result": "pass"}, "silent": {"result": "pass", "days_fired": 3}}


@pytest.mark.parametrize(
    "status, checks, fires, health",
    [
        ("disabled", RUNS_FAIL, 0, "disabled"),
        ("active", RUNS_FAIL, 5, "fails_to_run"),
        ("active", SEES_SILENT, 0, "data_silent"),
        ("active", SEES_FIELD, 0, "missing_field"),
        ("active", {}, 2, "fires"),
        ("active", ALERTED, 0, "fires"),
        ("active", NEVER, 0, "never_fired"),
        ("active", {"runs": {"result": "pass"}}, 0, "no_fire_data"),
    ],
)
def test_health_is_the_first_that_holds(status, checks, fires, health):
    assert catalog._health(status, checks, fires)[0] == health


def test_why_is_the_reason_line_and_raw_keeps_it_whole():
    state, why, raw = catalog._health("active", RUNS_FAIL, 0)
    # a bare error code takes the line that says what went wrong
    assert why == "Verification_exception: line 1:34: Unknown column [x]"
    assert raw == RUNS_FAIL["runs"]["reason"]
    assert catalog._health("active", SEES_SILENT, 0)[1] == "No events in idx-* in 7d"


@pytest.fixture
def built(monkeypatch):
    copies = {
        "rows": [
            _copy(1, "esql broken", "elastic", checks=RUNS_FAIL, techniques=["T1059"]),
            _copy(2, "stale index", "elastic", checks=SEES_SILENT),
            _copy(3, "macro gone", "splunk", checks=RUNS_FAIL),
            _copy(4, "quiet one", "wazuh", checks=NEVER),
            _copy(5, "loud one", "wazuh", checks=ALERTED),
            _copy(6, "off", "splunk", status="disabled"),
            *[
                _copy(100 + i, f"repo rule {i}", "repo:abc")
                for i in range(catalog.RULE_ROWS + 5)
            ],
        ],
        "rule_of": {},
        "blind_spots": [{"data_source": "idx-*", "tool": "elastic", "rules": 1}],
    }
    copies["rule_of"] = {r.id: (r.id,) for r in copies["rows"]}
    health = catalog._health_rows(copies, fires={})
    monkeypatch.setattr(
        catalog,
        "_listing_now",
        lambda: {**health, "blind_spots": copies["blind_spots"]},
    )


def _names(listing):
    return [r["name"] for r in listing["rules"]]


def test_attention_lists_what_to_act_on_worst_first(built):
    listing = catalog.health_listing("attention")
    assert _names(listing) == ["esql broken", "macro gone", "stale index", "quiet one"]
    assert listing["counts"]["no_fire_data"] == catalog.RULE_ROWS + 5


def test_tool_narrows_rows_and_counts(built):
    listing = catalog.health_listing(tool="splunk")
    assert _names(listing) == ["macro gone", "off"]
    assert listing["counts"]["fails_to_run"] == 1
    assert listing["counts"]["fires"] == 0
    assert listing["total"] == catalog.RULE_ROWS + 11


def test_search_reads_name_reason_and_technique(built):
    assert _names(catalog.health_listing(q="t1059")) == ["esql broken"]
    assert _names(catalog.health_listing(q="IDX-*")) == ["stale index"]


def test_pages_continue_from_offset(built):
    first = catalog.health_listing("no_fire_data")
    rest = catalog.health_listing("no_fire_data", offset=catalog.RULE_ROWS)
    assert first["matched"] == catalog.RULE_ROWS + 5
    assert len(first["rules"]) == catalog.RULE_ROWS
    assert len(rest["rules"]) == 5
    assert not {r["rule_id"] for r in first["rules"]} & {
        r["rule_id"] for r in rest["rules"]
    }


def test_by_siem_counts_enabled_copies(built):
    by_siem = catalog.health_listing()["by_siem"]
    assert by_siem["elastic"] == {
        "rules": 2,
        "cant_fire": 2,
        "not_checked": 0,
        "can_fire": 0,
    }
    assert by_siem["splunk"]["rules"] == 1
    assert by_siem["wazuh"] == {
        "rules": 2,
        "cant_fire": 0,
        "not_checked": 0,
        "can_fire": 2,
    }


def test_search_reads_the_reason_text(built):
    # "unknown column" is only in the two failing checks' reasons, not a name
    assert _names(catalog.health_listing(q="unknown column")) == [
        "esql broken",
        "macro gone",
    ]


def test_tool_and_search_combine(built):
    assert _names(catalog.health_listing(tool="splunk", q="macro")) == ["macro gone"]
    assert _names(catalog.health_listing(tool="elastic", q="macro")) == []


# --- the cache: rebuilt only when a table it reads was written ---------------


@pytest.fixture
def cache(monkeypatch):
    """_listing_now over stub reads: which marks Postgres reports, and how often
    the copies and the fire counts are read."""
    from contextlib import contextmanager

    state = {
        "marks": {
            "detection_rules": ("detection_rules", 1, 0, 0),
            "detection_rule_versions": ("detection_rule_versions", 1, 0, 0),
            "detection_rule_check_changes": ("detection_rule_check_changes", 0, 0, 0),
            "findings": ("findings", 0, 0, 0),
        },
        "copies_read": 0,
        "fires_read": 0,
        "rows": [_copy(1, "only rule", "elastic", checks=RUNS_FAIL)],
    }

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    def copies(_session):
        state["copies_read"] += 1
        rows = list(state["rows"])
        return {"rows": rows, "rule_of": {r.id: (r.id,) for r in rows}, "blind_spots": []}

    def fires(_session):
        state["fires_read"] += 1
        return {}

    monkeypatch.setattr(catalog, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(
        catalog,
        "_write_marks",
        lambda _s, tables: tuple(state["marks"][t] for t in sorted(tables)),
    )
    monkeypatch.setattr(catalog, "_copies_listed", copies)
    monkeypatch.setattr(catalog, "_fires", fires)
    monkeypatch.setattr(catalog, "_listing", {})
    return state


def _bump(state, table):
    name, ins, upd, dele = state["marks"][table]
    state["marks"][table] = (name, ins, upd + 1, dele)


def test_reads_reuse_the_built_listing_until_a_write(cache):
    for _ in range(3):
        catalog.health_listing("attention")
    assert (cache["copies_read"], cache["fires_read"]) == (1, 1)


@pytest.mark.parametrize(
    "table",
    ["detection_rules", "detection_rule_versions", "detection_rule_check_changes"],
)
def test_a_rules_or_checks_write_rebuilds_the_rows(cache, table):
    assert _names(catalog.health_listing()) == ["only rule"]
    cache["rows"].append(_copy(2, "new rule", "elastic", checks=RUNS_FAIL))
    _bump(cache, table)
    assert _names(catalog.health_listing()) == ["only rule", "new rule"]
    assert cache["copies_read"] == 2


def test_a_findings_write_recounts_fires_only(cache):
    catalog.health_listing()
    _bump(cache, "findings")
    catalog.health_listing()
    assert (cache["copies_read"], cache["fires_read"]) == (1, 2)


def test_a_new_day_recounts_fires(cache, monkeypatch):
    from datetime import datetime, timedelta, timezone

    catalog.health_listing()
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow

    monkeypatch.setattr(catalog, "datetime", _Clock)
    catalog.health_listing()
    assert (cache["copies_read"], cache["fires_read"]) == (1, 2)


# --- the route ----------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """GET /api/detection-rules/rules on a bare app, its permission gate waved
    through; the listing is stubbed to echo what it was asked."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.api.routers import detection_rules

    asked = []

    def listing(health=None, tool=None, q=None, offset=0):
        asked.append({"health": health, "tool": tool, "q": q, "offset": offset})
        return {"rules": []}

    monkeypatch.setattr(detection_rules.catalog, "health_listing", listing)
    app = FastAPI()
    app.include_router(detection_rules.router, prefix="/api/detection-rules")
    for dep in detection_rules.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    return TestClient(app), asked


def test_the_route_passes_its_filters_through(client):
    http, asked = client
    r = http.get(
        "/api/detection-rules/rules",
        params={"health": "attention", "tool": "splunk", "q": "macro", "offset": 300},
    )
    assert r.status_code == 200
    assert asked == [{"health": "attention", "tool": "splunk", "q": "macro", "offset": 300}]


@pytest.mark.parametrize(
    "params",
    [
        {"health": "healthy"},
        {"tool": "overlay"},
        {"offset": -1},
        {"q": "x" * 201},
    ],
)
def test_the_route_refuses_what_it_does_not_list(client, params):
    http, asked = client
    assert http.get("/api/detection-rules/rules", params=params).status_code == 422
    assert asked == []
