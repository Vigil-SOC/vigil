"""A rule search that misses the current view says how many it matches in the
views the chips leave out, so a healthy rule never reads as "no such rule"."""

from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog

RUNS_FAIL = {"runs": {"result": "fail", "reason": "failed: bad query"}}


def _copy(rule_id, name, source, checks=None):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        language="kql",
        source=source,
        status="active",
        techniques=[],
        checks=checks or {},
    )


@pytest.fixture
def built(monkeypatch):
    copies = {
        "rows": [
            _copy(1, "Certutil download broken", "elastic", RUNS_FAIL),
            _copy(2, "Certutil downloading a remote file", "repo:overlay"),
            _copy(3, "Certutil quiet copy", "splunk"),
            _copy(4, "Rundll32 parent", "repo:overlay"),
        ],
        "rule_of": {},
        "blind_spots": [],
    }
    copies["rule_of"] = {r.id: (r.id,) for r in copies["rows"]}
    hit = {"finding_id": "f", "title": "t", "severity": "low", "timestamp": None}
    health = catalog._health_rows(
        copies, fires={"by_name": {"Certutil downloading a remote file": [hit] * 6}}
    )
    monkeypatch.setattr(catalog, "_listing_now", lambda: {**health, "blind_spots": []})


def test_a_healthy_rule_searched_from_needs_attention_is_counted_elsewhere(built):
    listing = catalog.health_listing("attention", q="downloading a remote")
    assert listing["matched"] == 0
    assert listing["matched_elsewhere"] == {"fires": 1}


def test_matches_here_and_elsewhere_are_kept_apart(built):
    listing = catalog.health_listing("attention", q="certutil")
    assert [r["name"] for r in listing["rules"]] == ["Certutil download broken"]
    assert listing["matched_elsewhere"] == {"fires": 1, "no_fire_data": 1}


def test_other_views_follow_the_siem_filter(built):
    listing = catalog.health_listing("attention", tool="splunk", q="certutil")
    assert listing["matched"] == 0
    assert listing["matched_elsewhere"] == {"no_fire_data": 1}


def test_all_rules_and_no_search_have_nothing_elsewhere(built):
    assert catalog.health_listing(q="certutil")["matched_elsewhere"] == {}
    assert catalog.health_listing("attention")["matched_elsewhere"] == {}
