"""The Detections rule listing shows every check main stores, not only Runs and
Sees: a rule's failing Noisy, Quiet, Accurate, Silent or drift check is a flag,
a passing Caught is shown too, the worst problem across them all heads the row,
and each can be filtered and counted."""

from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog
from services.api.routers import detection_rules as routes

PASS = {"result": "pass"}
FAIL = {"result": "fail", "reason": "x"}


def _copy(rule_id, checks, status="active", source="elastic", name=None):
    return SimpleNamespace(
        id=rule_id,
        name=name or f"Rule {rule_id}",
        language="kql",
        source=source,
        status=status,
        techniques=[],
        checks=checks,
    )


def _built(copies, fires=None):
    return catalog._health_rows(
        {"rows": copies, "rule_of": {c.id: c.id for c in copies}}, fires or {}
    )


def _row(checks, status="active", fires=0):
    hit = {"finding_id": "f", "title": "t", "severity": "low", "timestamp": None}
    built = _built([_copy(1, checks, status)], {"by_name": {"Rule 1": [hit] * fires}})
    (row,) = [
        r for state in catalog.HEALTH_ORDER for _t, _h, r in built["by_state"][state]
    ]
    return row


def test_failing_flag_checks_are_flags_worst_first():
    row = _row(
        {"runs": PASS, "drift": FAIL, "quiet": FAIL, "noisy": FAIL, "accurate": PASS},
        fires=3,
    )
    assert row["flags"] == ["noisy", "quiet", "drift"]
    assert row["worst"] == "noisy"
    assert row["health"] == "fires"


@pytest.mark.parametrize("key", catalog.FLAG_CHECKS)
def test_each_flag_check_heads_a_firing_rule(key):
    row = _row({"runs": PASS, key: FAIL}, fires=2)
    assert row["flags"] == [key]
    assert row["worst"] == key


def test_unknown_or_passing_checks_are_no_flag():
    row = _row({"runs": PASS, "quiet": {"result": "unknown"}, "silent": PASS}, fires=1)
    assert row["flags"] == []
    assert row["worst"] == "fires"


def test_a_rule_that_cant_fire_stays_worst_whatever_its_flags():
    row = _row({"runs": FAIL, "drift": FAIL})
    assert row["worst"] == "fails_to_run"
    assert row["flags"] == ["drift"]


def test_a_disabled_rule_reads_disabled():
    assert _row({"drift": FAIL}, status="disabled")["worst"] == "disabled"


def test_caught_heads_a_rule_with_no_failing_flag():
    row = _row({"runs": PASS, "caught": PASS}, fires=4)
    assert row["caught"] is True
    assert row["worst"] == "caught"


def test_a_failing_flag_beats_caught():
    row = _row({"runs": PASS, "caught": PASS, "quiet": FAIL}, fires=4)
    assert row["worst"] == "quiet"
    assert row["caught"] is True


def test_untested_caught_is_not_caught():
    assert (
        _row({"runs": PASS, "caught": {"result": "untested"}}, fires=1)["caught"]
        is False
    )


def test_flag_counts_per_tool():
    built = _built(
        [
            _copy(1, {"runs": PASS, "quiet": FAIL}),
            _copy(2, {"runs": PASS, "quiet": FAIL, "drift": FAIL}, source="splunk"),
            _copy(3, {"runs": PASS, "caught": PASS}),
        ]
    )
    assert built["flag_counts"][None]["quiet"] == 2
    assert built["flag_counts"][None]["drift"] == 1
    assert built["flag_counts"][None]["caught"] == 1
    assert built["flag_counts"]["splunk"] == {
        **dict.fromkeys(catalog.FLAGS, 0),
        "quiet": 1,
        "drift": 1,
    }


@pytest.fixture
def listing(monkeypatch):
    copies = [
        _copy(1, {"runs": FAIL}),
        _copy(2, {"runs": PASS, "silent": {"result": "pass", "days_fired": 0}}),
        _copy(3, {"runs": PASS, "noisy": FAIL}),
        _copy(4, {"runs": PASS, "drift": FAIL, "caught": PASS}),
        _copy(5, {"runs": PASS, "caught": PASS}),
        _copy(6, {"runs": PASS}),
    ]
    hit = {"finding_id": "f", "title": "t", "severity": "low", "timestamp": None}
    named = {"Rule 3": 9, "Rule 4": 2, "Rule 5": 1, "Rule 6": 3}
    built = _built(copies, {"by_name": {k: [hit] * n for k, n in named.items()}})
    built["blind_spots"] = []
    monkeypatch.setattr(catalog, "_listing_now", lambda: built)


def test_worst_first_ranks_flags_between_cant_fire_and_never_fired(listing):
    rows = catalog.health_listing()["rules"]
    assert [(r["rule_id"], r["worst"]) for r in rows] == [
        (1, "fails_to_run"),
        (3, "noisy"),
        (4, "drift"),
        (2, "never_fired"),
        (6, "fires"),
        (5, "caught"),
    ]


@pytest.mark.parametrize(
    "flag, ids", [("noisy", [3]), ("drift", [4]), ("caught", [4, 5]), ("quiet", [])]
)
def test_flag_filter(listing, flag, ids):
    out = catalog.health_listing(flag=flag)
    assert [r["rule_id"] for r in out["rules"]] == ids
    assert out["matched"] == len(ids)


def test_listing_reports_flag_counts(listing):
    counts = catalog.health_listing()["flag_counts"]
    assert counts["noisy"] == 1
    assert counts["drift"] == 1
    assert counts["caught"] == 2


def test_the_route_passes_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        catalog, "health_listing", lambda *a, **kw: seen.update(kw) or {}
    )
    routes.get_rules(
        health=None, tool=None, q=None, offset=0, sort="worst", flag="drift", rules=None
    )
    assert seen["flag"] == "drift"
