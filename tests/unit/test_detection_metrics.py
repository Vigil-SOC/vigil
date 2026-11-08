"""The rollout's success metrics, computed from stored rows: each section's
numbers on hand-built rows, and the agent tool and API route that report them."""

import asyncio
from datetime import datetime, timedelta, timezone

from core.detections.inventory import metrics

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=6)


def _h(hours):
    return T0 + timedelta(hours=hours)


def _rule(name, techniques, validated, *shadow_hours):
    return {
        "name": name,
        "techniques": set(techniques),
        "validated": validated,
        "shadow_ats": [_h(h) for h in shadow_hours],
    }


# 1. Confirmed gap -> validated rule in shadow


def test_a_gap_closes_at_the_first_validated_rule_in_shadow_after_it():
    gaps = {"T1059.001": {"at": _h(0), "runs": 2}}
    rules = [
        _rule("unvalidated", ["T1059.001"], False, 1),
        _rule("validated, parent", ["T1059"], True, 5, 40),
        _rule("validated, later", ["T1059.001"], True, 30),
    ]
    out = metrics.gap_to_shadow(gaps, rules, NOW)
    row = out["rows"][0]
    assert (row["rule"], row["hours"], row["runs"]) == ("validated, parent", 5.0, 2)
    assert (out["gaps"], out["closed"], out["within_target"]) == (1, 1, 1)
    assert out["median_hours"] == 5.0


def test_a_rule_in_shadow_before_the_gap_or_on_another_technique_leaves_it_open():
    gaps = {"T1003": {"at": _h(10), "runs": 1}}
    rules = [
        _rule("before the gap", ["T1003"], True, 2),
        _rule("other technique", ["T1059"], True, 12),
    ]
    out = metrics.gap_to_shadow(gaps, rules, NOW)
    assert out["rows"][0]["rule"] is None
    assert out["rows"][0]["open_hours"] == 134.0
    assert (out["closed"], out["median_hours"]) == (0, None)


def test_a_gap_closed_after_a_day_is_not_within_target():
    gaps = {"T1021": {"at": _h(0), "runs": 1}}
    out = metrics.gap_to_shadow(gaps, [_rule("r", ["T1021.002"], True, 30)], NOW)
    assert (out["closed"], out["within_target"]) == (1, 0)
    assert "waiting for a person" in " ".join(out["unknown"])


# 2. Shadow -> live


def test_shadow_to_live_per_version_with_who_promoted_it():
    stages = [
        (1, 1, "A", 1, "shadow", "system", "new rule", _h(0)),
        (2, 1, "A", 1, "live", "system", "7 days in shadow, ...", _h(168)),
        (3, 1, "A", 2, "shadow", "system", "new version", _h(200)),
        (4, 2, "B", 1, "shadow", "system", "new rule", _h(10)),
        (5, 2, "B", 1, "live", "d.lee", "promoted by hand", _h(34)),
        (6, 3, "C", 1, "shadow", "system", "new rule", _h(0)),
        (7, 3, "C", 1, "off", "system", "3 failed runs in a row", _h(5)),
        # A live row with no shadow row before it is no promotion.
        (8, 4, "D", 1, "live", "system", "", _h(1)),
    ]
    out = metrics.shadow_to_live(stages, NOW + timedelta(days=10))
    assert [(r["name"], r["version"], r["hours"]) for r in out["rows"]] == [
        ("A", 1, 168.0),
        ("B", 1, 24.0),
    ]
    assert out["promoted_by_vigil"] == 1
    assert out["median_hours"] == 96.0
    assert [(w["name"], w["version"]) for w in out["waiting"]] == [("A", 2)]
    assert out["in_shadow"] == 1
    assert "7 days in shadow" in out["exit_criteria"]
    # The sweep promotes only on a lint pass too, and says so in its reason.
    assert out["exit_criteria"].endswith("Sees and lint passed")


# 3. Live rules passing Runs and Sees everywhere they run


def test_a_rule_passes_only_when_every_copy_that_runs_passes_runs_and_sees():
    copies = [
        {"rule": (1,), "tool": "elastic", "runs": "pass", "sees": "pass"},
        {"rule": (1,), "tool": "splunk", "runs": "pass", "sees": "fail"},
        {"rule": (2,), "tool": "splunk", "runs": "pass", "sees": "pass"},
        {"rule": ("overlay", "X"), "tool": "overlay", "runs": "pass", "sees": "pass"},
        {"rule": (3,), "tool": "wazuh", "runs": "pass", "sees": None},
    ]
    spots = [{"data_source": "index=a", "rules": [{}, {}]}]
    out = metrics.live_health(copies, spots)
    assert (out["live_rules"], out["passing"], out["share"]) == (4, 2, 0.5)
    assert out["by_tool"]["splunk"] == {"copies": 2, "passing": 1}
    assert out["by_tool"]["wazuh"] == {"copies": 1, "passing": 0}
    assert (out["blind_spots"], out["rules_starved"]) == (1, 2)


def test_no_live_rule_has_no_share():
    assert metrics.live_health([], [])["share"] is None


# 4. Found in the first week


def test_first_week_counts_each_rule_once_per_kind_inside_the_week():
    changes = [
        (1, "a", "sees", "fail", "no events in 7 d", _h(5)),
        (1, "a", "sees", "fail", "no events in 7 d", _h(30)),
        (2, "b", "sees", "fail", "no events in 7 d", _h(24 * 8)),
        (3, "c", "silent", "pass", "no alerts in 30 d", _h(24 * 6)),
        (4, "d", "silent", "fail", "no alert for 7 d", _h(24 * 9)),
        (5, "e", "silent", "unknown", "rule in the inventory under 7 d", _h(2)),
        (6, "f", "sees", "pass", None, _h(3)),
    ]
    out = metrics.first_week(T0, changes, NOW + timedelta(days=5))
    assert (out["no_data"], out["never_firing"], out["silent"]) == (1, 1, 0)
    assert out["first_found_day"] == {"no_data": 1, "never_firing": 7, "silent": 10}
    assert out["week_over"] is True
    assert [r["rule_id"] for r in out["rows"]["no_data"]] == [1]


def test_first_week_with_no_siem_read_yet():
    out = metrics.first_week(None, [], NOW)
    assert (out["started_at"], out["week_over"], out["no_data"]) == (None, False, 0)


# 5. Searches within budget, and throttle errors


def test_share_within_budget_counts_searches_over_a_lowered_limit():
    out = metrics.search_budget(
        {"splunk": 12, "elastic-siem": 4}, {"splunk": 10, "elastic-siem": 30}, []
    )
    assert out["integrations"]["splunk"] == {
        "searches_last_hour": 12,
        "max_searches_per_hour": 10,
    }
    assert out["share_within_budget"] == round(14 / 16, 3)
    assert metrics.search_budget({}, {"splunk": 30}, [])["share_within_budget"] is None


def test_throttle_errors_are_vigils_own_searches_one_per_minute():
    at = _h(1)
    throttled = [
        (1, "splunk", "quiet", "unknown", "HTTP 429 Too Many Requests", at),
        (
            2,
            "splunk",
            "quiet",
            "unknown",
            "HTTP 429 Too Many Requests",
            at + timedelta(seconds=20),
        ),
        (3, "elastic", "sees", "unknown", "es_rejected_execution_exception", at),
        # A Kibana rule's own run hitting a limit is the SIEM's, not Vigil's.
        (4, "elastic", "runs", "fail", "429 rate limit", at),
        # Vigil runs an overlay copy itself.
        (5, "overlay", "runs", "fail", "429 rate limit", at),
    ]
    out = metrics.search_budget({}, {}, throttled)
    assert out["throttle_errors"] == 3
    quiet = next(e for e in out["throttle_rows"] if e["check"] == "quiet")
    assert quiet["rules"] == 2


def test_throttle_pattern_reads_siem_refusals_and_not_ordinary_errors():
    for reason in (
        "HTTP 429",
        "status=429 Too Many Requests",
        "The maximum number of concurrent historical searches on this instance has been reached",
        "circuit_breaking_exception: [parent] Data too large",
        "rate limit exceeded",
        "search was throttled by the SIEM",
    ):
        assert metrics.throttle_error(reason), reason
    for reason in ("no events in 7 d", "lookup table missing", "version too new"):
        assert not metrics.throttle_error(reason), reason


def test_a_name_holding_a_throttle_word_is_no_throttle_error():
    for reason in (
        "fail: no events in index=waf_rate_limit in 7 d",
        "index throttle_audit not found",
        "no events in index=rate-limit-logs sourcetype=throttled:events in 7 d",
        "index=api_429 has no events in 7 d",
        "datamodel Throttling.Events not accelerated",
    ):
        assert not metrics.throttle_error(reason), reason
    rows = [(1, "splunk", "sees", "unknown", "index throttle_audit not found", _h(1))]
    reasons = [r for r in rows if metrics.throttle_error(r[4])]
    assert metrics.search_budget({}, {}, reasons)["throttle_errors"] == 0


def test_out_of_order_stage_rows_pair_by_time_and_never_go_negative():
    stages = [
        # Ids out of time order: the live row was written first.
        (1, 7, "E", 1, "live", "system", "7 days in shadow, ...", _h(170)),
        (2, 7, "E", 1, "shadow", "system", "new rule", _h(2)),
        # A live row stamped before its version's shadow row is no promotion.
        (3, 8, "F", 1, "shadow", "system", "new rule", _h(50)),
        (4, 8, "F", 1, "live", "system", "", _h(40)),
    ]
    out = metrics.shadow_to_live(stages, NOW + timedelta(days=10))
    assert [(r["name"], r["hours"]) for r in out["rows"]] == [("E", 168.0)]
    assert out["median_hours"] == 168.0
    assert all(r["hours"] >= 0 for r in out["rows"])
    # F's current stage is its last row by (at, id): the shadow row at 50 h.
    assert [(w["name"], w["in_shadow_hours"]) for w in out["waiting"]] == [("F", 334.0)]


def test_the_current_stage_is_the_last_row_in_time_order_not_by_id():
    stages = [
        # v2's shadow row was written first but stamped last.
        (5, 9, "H", 2, "shadow", "system", "new version", _h(300)),
        (6, 9, "H", 1, "shadow", "system", "new rule", _h(0)),
        (7, 9, "H", 1, "live", "system", "7 days in shadow, ...", _h(100)),
    ]
    out = metrics.shadow_to_live(stages, NOW + timedelta(days=20))
    assert [(r["name"], r["version"]) for r in out["rows"]] == [("H", 1)]
    assert [(w["name"], w["version"]) for w in out["waiting"]] == [("H", 2)]


def test_a_429_counts_only_as_a_status():
    for reason in (
        "status=429",
        "HTTP 429",
        "HTTP/1.1 429",
        "429 Too Many Requests",
        "search failed: error code: 429",
        "Status code 429 from the SIEM",
    ):
        assert metrics.throttle_error(reason), reason
    for reason in (
        "repo rule 429 not found",
        "429 alerts in 30 d (14/day), over 10/day",
        "rule 429 v2 is not in the inventory",
    ):
        assert not metrics.throttle_error(reason), reason


def test_source_is_a_name_only_with_equals_or_colon():
    assert metrics.throttle_error("upstream source rate limited the search")
    assert metrics.throttle_error("the source throttled the search")
    for reason in (
        "no events in source=rate_limit.log in 7 d",
        "source: throttle_feed has no events",
    ):
        assert not metrics.throttle_error(reason), reason


# The agent tool and the API route


def test_the_agent_tool_is_registered_as_a_read_and_returns_the_report(monkeypatch):
    from core.agents import tool_registry
    from core.detections.tools import SecurityDetectionsTools

    assert tool_registry.TOOL_PERMISSIONS["detection_metrics"] == "detections.read"
    assert "detection_metrics" in tool_registry._SECURITY_TOOLS
    assert "detection_metrics" in tool_registry.MANIFEST
    monkeypatch.setattr(metrics, "report", lambda: {"stub": True})
    out = asyncio.run(SecurityDetectionsTools().detection_metrics())
    assert out == {"stub": True}


def test_the_api_route_returns_the_report(monkeypatch):
    from services.api.routers import detection_rules

    monkeypatch.setattr(metrics, "report", lambda: {"stub": True})
    route = next(r for r in detection_rules.router.routes if r.path == "/metrics")
    assert "GET" in route.methods
    assert route.endpoint() == {"stub": True}


def test_report_puts_every_section_together(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Db:
        def session_scope(self):
            return Session()

    monkeypatch.setattr(metrics, "get_db_manager", lambda: Db())
    for name, value in (
        ("_overlay_rules", [_rule("r", ["T1003"], True, 3)]),
        ("_stages", []),
        ("_live_copies", []),
        ("_siem_start", T0),
        ("_siem_changes", []),
        ("_sent", {}),
        ("_throttled", []),
    ):
        monkeypatch.setattr(metrics, name, lambda *_a, v=value: v)
    monkeypatch.setattr(metrics, "_limits", lambda: {"splunk": 30})
    monkeypatch.setattr(metrics.catalog, "blind_spots", lambda: [])
    out = metrics.report(
        NOW, read_gaps=lambda days: {"T1003": {"at": _h(1), "runs": 1}}
    )
    assert set(out) == {
        "generated_at",
        "gap_to_shadow",
        "shadow_to_live",
        "live_health",
        "first_week",
        "search_budget",
    }
    assert out["gap_to_shadow"]["rows"][0]["hours"] == 2.0
    assert all("source" in v for k, v in out.items() if k != "generated_at")
