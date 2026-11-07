"""Rule fire counts on the Detections list: findings are traced to the rule that
fired them (not matched by title), the SIEM's own alert count counts too, Never
fired counts rules not copies, and a rule's count opens its findings."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog
from services.api.routers import detection_rules as routes


def _finding(fid, title, ctx=None, source="elastic", when=None):
    return SimpleNamespace(
        finding_id=fid,
        title=title,
        severity="high",
        timestamp=when or datetime(2026, 11, 5, 12, 0),
        data_source=source,
        entity_context=ctx,
    )


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        return SimpleNamespace(all=lambda: self.rows)


@pytest.fixture
def resolve(monkeypatch):
    """resolve_findings, answering from a {finding_id: rule_id} map."""
    traced = {}

    def fake(_session, findings, full=True):
        return {
            k: (
                {"resolved": True, "rule_id": traced[k]}
                if k in traced
                else {"resolved": False, "reason": "no rule id"}
            )
            for k in findings
        }

    monkeypatch.setattr(catalog, "resolve_findings", fake)
    return traced


def test_a_traced_finding_counts_on_its_rule_not_by_title(resolve):
    # A Wazuh SCA rule's name is a template; its alert's title never equals it.
    resolve["f1"] = 7
    resolve["f2"] = 7
    fires = catalog._fires(
        _Session([_finding("f1", "CIS: Ensure X"), _finding("f2", "CIS: Ensure Y")])
    )
    assert [h["finding_id"] for h in fires["by_rule"][7]] == ["f1", "f2"]
    assert fires["by_name"] == {}


def test_an_untraced_finding_falls_back_to_the_rule_name_then_title(resolve):
    fires = catalog._fires(
        _Session(
            [
                _finding("f1", "Alert title", {"rule_name": "Lab rule"}),
                _finding("f2", "Other rule"),
                _finding("f3", None),
            ]
        )
    )
    assert [h["finding_id"] for h in fires["by_name"]["Lab rule"]] == ["f1"]
    assert [h["finding_id"] for h in fires["by_name"]["Other rule"]] == ["f2"]
    assert fires["by_rule"] == {}


def test_finding_times_read_as_utc(resolve):
    resolve["f1"] = 7
    fires = catalog._fires(
        _Session([_finding("f1", "t", when=datetime(2026, 11, 4, 23, 11))])
    )
    assert fires["by_rule"][7][0]["timestamp"] == "2026-11-04T23:11:00+00:00"


def test_no_findings_reads_nothing(resolve):
    assert catalog._fires(_Session([])) == {"by_rule": {}, "by_name": {}}


def _copy(rule_id, name, checks=None, status="active"):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        language="kql",
        source="wazuh",
        status=status,
        techniques=[],
        checks=checks or {},
    )


def _built(copies, fires, rule_of=None):
    return catalog._health_rows(
        {"rows": copies, "rule_of": rule_of or {c.id: c.id for c in copies}}, fires
    )


def _rows(built):
    return {
        r["rule_id"]: r
        for state in catalog.HEALTH_ORDER
        for _t, _h, r in built["by_state"][state]
    }


HIT = {"finding_id": "f", "title": "t", "severity": "low", "timestamp": None}


def test_a_rule_fires_on_findings_traced_to_it():
    built = _built(
        [_copy(1, "$(sca.policy): $(sca.check.title)")], {"by_rule": {1: [HIT] * 44}}
    )
    row = _rows(built)[1]
    assert row["fires"] == 44
    assert row["health"] == "fires"
    assert len(built["fired"][1]) == 44


def test_traced_and_name_matched_findings_add_up():
    built = _built(
        [_copy(1, "R")], {"by_rule": {1: [HIT]}, "by_name": {"R": [HIT] * 2}}
    )
    assert _rows(built)[1]["fires"] == 3


def test_the_siem_count_fires_a_rule_no_finding_names():
    checks = {"quiet": {"result": "pass", "count": 71, "days": 29}}
    row = _rows(_built([_copy(1, "R", checks)], {}))[1]
    assert row["health"] == "fires"
    assert row["fires"] == 0
    assert (row["siem_alerts"], row["siem_days"]) == (71, 29)
    assert row["why"] == "The SIEM counted 71 alerts in 29 days; none became a finding."


@pytest.mark.parametrize(
    "checks, health",
    [
        (
            {"quiet": {"count": 0, "days": 30}, "silent": {"days_fired": 0}},
            "never_fired",
        ),
        ({"quiet": {"count": None}}, "no_fire_data"),
        ({"quiet": {"count": True}}, "no_fire_data"),
        ({}, "no_fire_data"),
    ],
)
def test_never_fired_needs_a_count_of_zero(checks, health):
    assert _rows(_built([_copy(1, "R", checks)], {}))[1]["health"] == health


def test_never_fired_counts_rules_once_not_copies():
    never = {"silent": {"result": "pass", "days_fired": 0}}
    built = _built(
        [_copy(1, "A", never), _copy(2, "A", never), _copy(3, "B", never)],
        {},
        rule_of={1: 100, 2: 100, 3: 300},
    )
    assert built["counts"][None]["never_fired"] == 3
    assert built["never_fired_rules"] == 2


def test_a_copy_keeps_its_newest_findings_for_the_click_through(monkeypatch):
    monkeypatch.setattr(catalog, "FIRES_LISTED", 2)
    hits = [dict(HIT, finding_id=f"f{i}") for i in range(5)]
    built = _built([_copy(1, "R")], {"by_rule": {1: hits}})
    assert [h["finding_id"] for h in built["fired"][1]] == ["f0", "f1"]
    assert _rows(built)[1]["fires"] == 5


@pytest.fixture
def listing(monkeypatch):
    built = _built([_copy(1, "R"), _copy(2, "S")], {"by_rule": {1: [HIT] * 3}})
    built["blind_spots"] = []
    monkeypatch.setattr(catalog, "_listing_now", lambda: built)


def test_rule_findings_lists_what_the_count_counts(listing):
    out = catalog.rule_findings(1)
    assert out["total"] == 3
    assert len(out["findings"]) == 3
    assert out["days"] == catalog.FIRES_WINDOW_DAYS


def test_rule_findings_of_a_rule_with_none(listing):
    assert catalog.rule_findings(2)["findings"] == []
    assert catalog.rule_findings(999) == {
        "rule_id": 999,
        "total": 0,
        "days": catalog.FIRES_WINDOW_DAYS,
        "findings": [],
    }


def test_the_listing_reports_never_fired_rules(listing):
    assert "never_fired_rules" in catalog.health_listing()


def test_the_findings_route(listing):
    assert routes.get_rule_findings(1)["total"] == 3


class _InventorySession(_Session):
    """Findings for _fires' read; the inventory for the real resolve_findings."""

    def __init__(self, rows, rule, versions):
        super().__init__(rows)
        self.rule, self.versions = rule, versions

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is catalog.DetectionRule:
            return [self.rule]
        if entity is catalog.DetectionRuleVersion:
            return list(self.versions)
        return []


def test_a_siem_finding_resolves_to_its_rule_by_its_alert_time():
    # The real resolve_findings: it picks the version by the alert's time, so
    # _fires must hand it the finding's timestamp.
    rule = SimpleNamespace(
        id=10893,
        source="elastic",
        native_key="rule-uuid-1",
        name="Lab rule",
        status="active",
        latest_version=1,
    )
    version = SimpleNamespace(
        rule_id=10893,
        version=1,
        first_seen=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    alert = {
        "source_evidence": {
            "records": [{"_source": {"kibana.alert.rule.rule_id": "rule-uuid-1"}}]
        }
    }
    session = _InventorySession(
        [_finding("f1", "Some alert title", alert, when=datetime(2026, 11, 5, 12, 0))],
        rule,
        [version],
    )
    fires = catalog._fires(session)
    assert [h["finding_id"] for h in fires["by_rule"][10893]] == ["f1"]
    assert fires["by_name"] == {}


def test_a_name_shared_by_two_copies_counts_on_neither():
    built = _built(
        [_copy(1, "Same name"), _copy(2, "Same name"), _copy(3, "Own name")],
        {"by_name": {"Same name": [HIT] * 4, "Own name": [HIT] * 2}},
    )
    rows = _rows(built)
    assert (rows[1]["fires"], rows[1]["fires_unsure"]) == (0, 4)
    assert (rows[2]["fires"], rows[2]["fires_unsure"]) == (0, 4)
    assert (rows[3]["fires"], rows[3]["fires_unsure"]) == (2, 0)


def test_a_shared_name_still_counts_findings_traced_to_each_copy():
    built = _built(
        [_copy(1, "Same name"), _copy(2, "Same name")],
        {"by_rule": {1: [HIT] * 3}, "by_name": {"Same name": [HIT]}},
    )
    rows = _rows(built)
    assert rows[1]["fires"] == 3
    assert rows[2]["fires"] == 0


def test_a_linked_copy_and_its_origin_share_a_name_as_one_rule():
    # SIEM copy 7688 links to repo rule 19829; both carry the rule's name.
    built = _built(
        [_copy(7688, "Same name"), _copy(19829, "Same name")],
        {"by_name": {"Same name": [HIT] * 3}},
        rule_of={7688: (19829,), 19829: (19829,)},
    )
    rows = _rows(built)
    assert (rows[7688]["fires"], rows[7688]["fires_unsure"]) == (3, 0)
    assert (rows[19829]["fires"], rows[19829]["fires_unsure"]) == (3, 0)
