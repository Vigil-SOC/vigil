"""A Case shows the proof card of each rule version that raised its alerts (#4013).

Its alerts are traced as Fires traces them (#3972): to the version
``resolve_findings`` maps each to, else by the rule name or title the alert
carries when exactly one firing copy carries it. One version is one card, with
its stored checks, worst problem and stage; alerts traced to none are counted.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.api.v1 import cases_router
from core.detections.inventory import catalog

WHEN = datetime(2026, 11, 5, 12, 0)
PASS = {"result": "pass"}
FAIL = {"result": "fail", "reason": "fail: too many alerts"}


def _finding(fid, title="Alert", ctx=None, source="elastic", when=WHEN):
    return SimpleNamespace(
        finding_id=fid,
        title=title,
        timestamp=when,
        data_source=source,
        entity_context=ctx,
    )


def _found(rule_id, version, checks=None, source="elastic", name=None, status="active"):
    return {
        "resolved": True,
        "rule_id": rule_id,
        "source": source,
        "native_key": f"key-{rule_id}",
        "name": name or f"Rule {rule_id}",
        "status": status,
        "version": version,
        "latest_version": 3,
        "first_seen": "2026-11-01T00:00:00+00:00",
        "query": "q",
        "checks": checks or {},
    }


class _Session:
    """The inventory reads: rules and versions by name; nothing else."""

    def __init__(self, rules=(), versions=()):
        self.rules, self.versions = list(rules), list(versions)

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is catalog.DetectionRule:
            return list(self.rules)
        if entity is catalog.DetectionRuleVersion:
            return list(self.versions)
        return []


@pytest.fixture
def resolve(monkeypatch):
    """resolve_findings answering from {finding_id: resolved dict}; stages too."""
    traced = {}
    stages = {}

    def fake(_session, findings, full=True):
        assert full, "a card needs the version's checks"
        return {
            k: traced.get(k, {"resolved": False, "reason": f"{k}: no rule id"})
            for k in findings
        }

    monkeypatch.setattr(catalog, "resolve_findings", fake)
    monkeypatch.setattr(
        catalog,
        "_stage_rows",
        lambda _s, ids: {i: stages[i] for i in ids if i in stages},
    )
    return SimpleNamespace(traced=traced, stages=stages)


def test_one_card_per_version_with_its_findings(resolve):
    resolve.traced.update(f1=_found(7, 2), f2=_found(7, 2), f3=_found(7, 3))
    out = catalog.case_rules(
        _Session(), [_finding("f1"), _finding("f2"), _finding("f3")]
    )
    cards = {(c["rule_id"], c["version"]): c for c in out["rules"]}
    assert set(cards) == {(7, 2), (7, 3)}
    assert cards[(7, 2)]["findings"] == ["f1", "f2"]
    assert cards[(7, 3)]["findings"] == ["f3"]
    assert cards[(7, 2)]["latest_version"] == 3
    assert out["unresolved"] == 0


def test_a_card_carries_the_versions_checks_flags_and_worst(resolve):
    checks = {
        "runs": PASS,
        "sees": PASS,
        "quiet": PASS,
        "accurate": PASS,
        "noisy": FAIL,
        "silent": PASS,
        "caught": PASS,
        "drift": PASS,
        "lint": PASS,
    }
    resolve.traced["f1"] = _found(7, 2, checks)
    (card,) = catalog.case_rules(_Session(), [_finding("f1")])["rules"]
    assert set(card["checks"]) == set(catalog.CARD_CHECKS)
    assert card["checks"]["noisy"] == FAIL
    assert card["flags"] == ["noisy"]
    assert card["caught"] is True
    assert card["health"] == "fires"
    assert card["worst"] == "noisy"
    assert card["stage"] is None
    assert card["tool"] == "elastic"


@pytest.mark.parametrize(
    "checks,worst",
    [
        ({"runs": {"result": "fail", "reason": "fail: bad query"}}, "fails_to_run"),
        ({"sees": {"result": "fail", "reason": "no data"}}, "data_silent"),
        ({"caught": PASS}, "caught"),
        ({}, "fires"),
    ],
    ids=["cant-run", "cant-see", "caught", "fires"],
)
def test_the_worst_problem_reads_the_versions_checks(resolve, checks, worst):
    resolve.traced["f1"] = _found(7, 2, checks)
    (card,) = catalog.case_rules(_Session(), [_finding("f1")])["rules"]
    assert card["worst"] == worst


def test_a_disabled_rule_is_shown_as_off(resolve):
    resolve.traced["f1"] = _found(7, 2, {"noisy": FAIL}, status="disabled")
    (card,) = catalog.case_rules(_Session(), [_finding("f1")])["rules"]
    assert card["worst"] == "disabled"


def test_cards_are_worst_first(resolve):
    resolve.traced.update(
        f1=_found(1, 1, {"caught": PASS}),
        f2=_found(2, 1, {"runs": {"result": "fail"}}),
        f3=_found(3, 1, {"quiet": FAIL}),
    )
    out = catalog.case_rules(
        _Session(), [_finding("f1"), _finding("f2"), _finding("f3")]
    )
    assert [c["worst"] for c in out["rules"]] == ["fails_to_run", "quiet", "caught"]


def test_an_overlay_card_shows_its_current_stage(resolve):
    resolve.traced["f1"] = _found(9, 2, source="repo:overlay")
    resolve.stages[9] = [{"stage": "shadow"}, {"stage": "live"}]
    (card,) = catalog.case_rules(_Session(), [_finding("f1")])["rules"]
    assert card["tool"] == "overlay"
    assert card["stage"] == "live"


def test_an_alert_naming_no_rule_is_counted_with_why(resolve):
    resolve.traced["f1"] = _found(7, 2)
    out = catalog.case_rules(
        _Session(), [_finding("f1"), _finding("f2", title="Nothing carries this")]
    )
    assert len(out["rules"]) == 1
    assert out["unresolved"] == 1
    assert out["unresolved_findings"] == [
        {"finding_id": "f2", "reason": "f2: no rule id"}
    ]


def _rule(rule_id, name, source="wazuh"):
    return SimpleNamespace(
        id=rule_id,
        name=name,
        source=source,
        native_key=f"key-{rule_id}",
        status="active",
        latest_version=2,
    )


def _version(rule_id, number, first_seen, checks=None):
    return SimpleNamespace(
        rule_id=rule_id,
        version=number,
        first_seen=first_seen,
        query="q",
        checks=checks or {},
    )


def test_an_untraced_alert_falls_back_to_its_rule_name_at_its_time(resolve):
    # v2 came after the alert, so v1 raised it.
    session = _Session(
        [_rule(5, "Lab rule")],
        [
            _version(5, 2, datetime(2026, 11, 6, tzinfo=timezone.utc)),
            _version(5, 1, datetime(2026, 11, 1, tzinfo=timezone.utc), {"quiet": FAIL}),
        ],
    )
    out = catalog.case_rules(session, [_finding("f1", ctx={"rule_name": "Lab rule"})])
    (card,) = out["rules"]
    assert (card["rule_id"], card["version"], card["by_name"]) == (5, 1, 1)
    assert card["worst"] == "quiet"
    assert out["unresolved"] == 0


def test_the_title_is_the_last_fallback(resolve):
    session = _Session(
        [_rule(5, "Lab rule")],
        [_version(5, 1, datetime(2026, 11, 1, tzinfo=timezone.utc))],
    )
    out = catalog.case_rules(session, [_finding("f1", title="Lab rule")])
    assert [c["rule_id"] for c in out["rules"]] == [5]


def test_a_repo_origin_sharing_the_name_is_not_a_second_carrier(resolve):
    session = _Session(
        [_rule(5, "Lab rule"), _rule(6, "Lab rule", source="repo:detections")],
        [_version(5, 1, datetime(2026, 11, 1, tzinfo=timezone.utc))],
    )
    out = catalog.case_rules(session, [_finding("f1", title="Lab rule")])
    assert [c["rule_id"] for c in out["rules"]] == [5]


def test_a_name_two_firing_copies_carry_traces_to_neither(resolve):
    session = _Session(
        [_rule(5, "Lab rule"), _rule(6, "Lab rule", source="splunk")],
        [_version(5, 1, datetime(2026, 11, 1, tzinfo=timezone.utc))],
    )
    out = catalog.case_rules(session, [_finding("f1", title="Lab rule")])
    assert out["rules"] == []
    assert out["unresolved"] == 1


def test_an_alert_older_than_every_version_stays_unresolved(resolve):
    session = _Session(
        [_rule(5, "Lab rule")],
        [_version(5, 1, datetime(2026, 11, 6, tzinfo=timezone.utc))],
    )
    out = catalog.case_rules(session, [_finding("f1", title="Lab rule")])
    assert out["rules"] == []
    assert out["unresolved_findings"][0]["finding_id"] == "f1"


def test_a_case_with_no_alerts_has_no_cards(resolve):
    assert catalog.case_rules(_Session(), []) == {
        "rules": [],
        "unresolved": 0,
        "unresolved_findings": [],
    }


def test_a_kibana_alert_resolves_through_the_real_mapping():
    # The real resolve_findings: the alert names its Kibana rule id.
    rule = _rule(10893, "Lab rule", source="elastic")
    version = _version(
        10893, 1, datetime(2026, 11, 1, tzinfo=timezone.utc), {"caught": PASS}
    )
    alert = {
        "source_evidence": {
            "records": [{"_source": {"kibana.alert.rule.rule_id": "key-10893"}}]
        }
    }
    session = _Session([rule], [version])
    (card,) = catalog.case_rules(session, [_finding("f1", ctx=alert)])["rules"]
    assert (card["rule_id"], card["version"], card["by_name"]) == (10893, 1, 0)
    assert card["worst"] == "caught"


class _RouteSession(_Session):
    def __init__(self, cases, rows):
        super().__init__()
        self.cases, self.rows = cases, rows

    def get(self, _model, case_id):
        return object() if case_id in self.cases else None

    def execute(self, _statement):
        return SimpleNamespace(all=lambda: self.rows)


def test_the_route_returns_the_cases_cards(resolve):
    resolve.traced["f1"] = _found(7, 2, {"quiet": FAIL})
    session = _RouteSession({"c-1"}, [_finding("f1"), _finding("f2")])
    out = cases_router.get_case_rules("c-1", session)
    body = cases_router.CaseRulesResponse.model_validate(out)
    assert [(c.rule_id, c.version, c.worst) for c in body.rules] == [(7, 2, "quiet")]
    assert body.unresolved == 1


def test_the_route_404s_an_unknown_case(resolve):
    with pytest.raises(HTTPException) as raised:
        cases_router.get_case_rules("nope", _RouteSession(set(), []))
    assert raised.value.status_code == 404
