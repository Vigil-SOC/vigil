"""The Detections rule listing names where each rule reads its data (its Sees
check's data source), finds rules by it, and sorts by fires or name as well as
worst first."""

from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog
from services.api.routers import detection_rules as routes


def _copy(rule_id, name, source="elastic", checks=None, techniques=()):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        language="kql",
        source=source,
        status="active",
        techniques=list(techniques),
        checks=checks,
    )


def _rows(copies, fires=None):
    built = catalog._health_rows(
        {"rows": copies, "rule_of": {c.id: c.id for c in copies}}, fires or {}
    )
    return [
        row
        for state in catalog.HEALTH_ORDER
        for _t, _h, row in built["by_state"][state]
    ]


@pytest.mark.parametrize(
    "checks, where",
    [
        (
            {"sees": {"result": "pass", "data_source": "logs-endpoint.*"}},
            "logs-endpoint.*",
        ),
        (
            {"sees": {"result": "fail", "data_source": "index=o365 sourcetype=x"}},
            "index=o365 sourcetype=x",
        ),
        ({"sees": {"result": "unknown", "data_source": None}}, None),
        ({"sees": {"result": "pass", "data_source": "  "}}, None),
        ({"runs": {"result": "pass"}}, None),
        (None, None),
    ],
)
def test_where_is_the_sees_data_source(checks, where):
    (row,) = _rows([_copy(1, "R", checks=checks)])
    assert row["where"] == where


def test_where_is_not_the_tool_twice():
    (row,) = _rows(
        [
            _copy(
                1,
                "R",
                "splunk",
                {"sees": {"result": "pass", "data_source": "index=sysmon"}},
            )
        ]
    )
    assert row["tool"] == "splunk"
    assert row["where"] == "index=sysmon"


def test_where_drops_a_leading_tool_name():
    checks = {
        "sees": {"result": "pass", "data_source": "wazuh decoder windows_eventchannel"}
    }
    (row,) = _rows([_copy(1, "R", "wazuh", checks)])
    assert row["where"] == "decoder windows_eventchannel"
    (row,) = _rows([_copy(2, "R", "elastic", checks)])
    assert row["where"] == "wazuh decoder windows_eventchannel"


def test_search_finds_a_rule_by_its_data_source():
    row = {
        "name": "R",
        "why": "fires",
        "where": "wazuh-alerts-*",
        "techniques": ["T1003"],
    }
    assert "wazuh-alerts-*" in catalog._haystack(row)
    row["where"] = None
    assert catalog._haystack(row) == "r\nfires\n\nt1003"


@pytest.fixture
def listing(monkeypatch):
    def row(rule_id, name, fires, health="fires"):
        return (
            "elastic",
            name.lower(),
            {
                "rule_id": rule_id,
                "name": name,
                "fires": fires,
                "health": health,
                "worst": health,
                "flags": [],
                "caught": False,
            },
        )

    built = {
        "total": 5,
        "rule_total": 5,
        "mapped": 0,
        "counts": {None: dict.fromkeys(catalog.HEALTH_ORDER, 0)},
        "flag_counts": {None: dict.fromkeys(catalog.FLAGS, 0)},
        "by_state": {state: [] for state in catalog.HEALTH_ORDER},
        "by_siem": {},
        "blind_spots": [],
    }
    built["by_state"]["fails_to_run"] = [row(5, "beta", 0, "fails_to_run")]
    built["by_state"]["fires"] = [
        row(1, " Delta", 40),
        row(2, "alpha", 3),
        row(3, "Gamma", 40),
    ]
    built["by_state"]["never_fired"] = [row(4, "epsilon", 0, "never_fired")]
    monkeypatch.setattr(catalog, "_listing_now", lambda: built)


@pytest.mark.parametrize(
    "sort, ids",
    [
        ("worst", [5, 4, 1, 2, 3]),
        ("most_fires", [1, 3, 2, 4, 5]),
        ("fewest_fires", [4, 5, 2, 1, 3]),
        ("name", [2, 5, 1, 4, 3]),
    ],
)
def test_listing_sorts(listing, sort, ids):
    out = catalog.health_listing(sort=sort)
    assert [r["rule_id"] for r in out["rules"]] == ids


def test_sort_pages_from_the_sorted_list(listing, monkeypatch):
    monkeypatch.setattr(catalog, "RULE_ROWS", 2)
    first = catalog.health_listing(sort="name")["rules"]
    second = catalog.health_listing(sort="name", offset=2)["rules"]
    assert [r["rule_id"] for r in first + second] == [2, 5, 1, 4]


def test_sort_keeps_the_filters(listing):
    out = catalog.health_listing(health="fires", sort="fewest_fires")
    assert [r["rule_id"] for r in out["rules"]] == [2, 1, 3]


def test_the_route_passes_sort(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        catalog, "health_listing", lambda *a, **kw: seen.update(kw) or {}
    )
    routes.get_rules(health=None, tool=None, q=None, offset=0, sort="name")
    assert seen["sort"] == "name"
