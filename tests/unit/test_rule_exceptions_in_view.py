"""A healthy rule's active exceptions stay in sight from the default view: each
row says how many it has and what they silenced, a filter lists every rule with
one whatever its health, and a view that leaves some out says how many."""

from datetime import date
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog
from services.api.routers import detection_rules as routes

PASS = {"result": "pass"}
RUNS_FAIL = {"runs": {"result": "fail", "reason": "failed: bad query"}}


def _copy(rule_id, name, source="repo:overlay", checks=None):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        language="esql",
        source=source,
        status="active",
        techniques=[],
        checks=checks or {"runs": PASS, "sees": PASS},
    )


def _days(*counts):
    return {"days": {f"2026-11-0{i + 1}": n for i, n in enumerate(counts)}}


@pytest.fixture
def built(monkeypatch):
    healthy = {
        "runs": PASS,
        "sees": PASS,
        "exceptions": {"11": _days(200, 10), "12": _days(130), "99": _days(5000)},
    }
    copies = {
        "rows": [
            _copy(1, "Encoded PowerShell from Office", "elastic", RUNS_FAIL),
            _copy(2, "Scheduled task from a temp folder", checks=healthy),
            _copy(3, "Admin share from a workstation"),
            _copy(
                4,
                "Certutil broken",
                checks={**RUNS_FAIL, "exceptions": {"21": _days(7)}},
            ),
            _copy(5, "Rundll32 parent", "splunk"),
        ],
        # 99 was removed: its counts stay in checks but it isn't active.
        "exceptions": {2: [11, 12], 3: [31], 4: [21]},
        "blind_spots": [],
    }
    copies["rule_of"] = {r.id: (r.id,) for r in copies["rows"]}
    health = catalog._health_rows(
        copies, fires={"Scheduled task from a temp folder": 4}
    )
    monkeypatch.setattr(catalog, "_listing_now", lambda: {**health, "blind_spots": []})
    return health


def _row(listing, rule_id):
    (row,) = [r for r in listing["rules"] if r["rule_id"] == rule_id]
    return row


def test_a_row_says_how_many_active_exceptions_and_what_they_silenced(built):
    listing = catalog.health_listing()
    assert (_row(listing, 2)["exceptions"], _row(listing, 2)["silenced"]) == (2, 340)
    assert (_row(listing, 3)["exceptions"], _row(listing, 3)["silenced"]) == (1, 0)
    assert (_row(listing, 5)["exceptions"], _row(listing, 5)["silenced"]) == (0, 0)


def test_the_default_view_says_how_many_rules_with_exceptions_it_leaves_out(built):
    listing = catalog.health_listing("attention")
    assert {r["rule_id"] for r in listing["rules"]} == {1, 4}
    assert listing["with_exceptions"] == 3
    # 2 and 3 are healthy, so Needs attention leaves them and their exceptions out.
    assert listing["exceptions_elsewhere"] == 2


def test_has_exceptions_lists_every_rule_with_one_whatever_its_health(built):
    listing = catalog.health_listing(has_exceptions=True)
    assert [r["rule_id"] for r in listing["rules"]] == [4, 2, 3]
    assert listing["matched"] == 3
    assert listing["exceptions_elsewhere"] == 0
    # A health asked for alongside does not narrow it.
    assert catalog.health_listing("attention", has_exceptions=True)["matched"] == 3


def test_exceptions_follow_the_siem_filter_and_the_search(built):
    assert catalog.health_listing("attention", tool="splunk")["with_exceptions"] == 0
    assert (
        catalog.health_listing("attention", tool="splunk")["exceptions_elsewhere"] == 0
    )
    searched = catalog.health_listing("attention", q="scheduled")
    assert searched["exceptions_elsewhere"] == 1
    assert catalog.health_listing(has_exceptions=True, q="admin")["matched"] == 1


def test_an_exception_added_or_removed_rebuilds_the_listing():
    assert "detection_rule_exceptions" in catalog._LISTING_TABLES


def test_the_rules_route_passes_has_exceptions(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        catalog, "health_listing", lambda *a, **kw: seen.update(kw) or {"rules": []}
    )
    routes.get_rules(
        health=None,
        tool=None,
        q=None,
        offset=0,
        sort="worst",
        flag=None,
        rules=None,
        has_exceptions=True,
    )
    assert seen["has_exceptions"] is True


def test_silenced_counts_only_the_runners_14_days():
    # The window is today and the 13 UTC days before it, as the exceptions panel
    # counts it; a day the runner hasn't trimmed yet falls outside.
    checks = {
        "runs": PASS,
        "exceptions": {
            "41": {"days": {"2026-10-23": 3, "2026-10-25": 2, "2026-11-06": 25}},
        },
    }
    copies = {
        "rows": [_copy(9, "Certutil downloading a remote file", checks=checks)],
        "exceptions": {9: [41]},
        "blind_spots": [],
        "rule_of": {9: (9,)},
    }
    health = catalog._health_rows(copies, fires={}, today=date(2026, 11, 7))
    (row,) = [
        r for state in catalog.HEALTH_ORDER for _t, _h, r in health["by_state"][state]
    ]
    # 2026-10-23 is 15 days old: out. 2026-10-25, the window's first day: in.
    assert row["silenced"] == 27
