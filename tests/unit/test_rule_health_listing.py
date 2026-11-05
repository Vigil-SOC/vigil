"""The Detections rule listing: each copy's health and reason, and the filters
and pages read from the built rows."""

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
