"""A Wazuh overlay rule in shadow is run as a search over wazuh-archives-*:
the translation, and what the run records for a rule it can or cannot validate."""

from datetime import datetime, timedelta, timezone

import pytest

from core.detections.inventory import rollout, wazuh_shadow
from core.detections.inventory.wazuh_shadow import translate

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _rule(body, attrs=""):
    return (
        f'<rule id="100100" level="5"{attrs}>{body}<description>d</description></rule>'
    )


def _filters(body):
    got = translate(_rule(body))
    assert got.reasons == [], got.reasons
    return got.query["bool"]["filter"]


@pytest.mark.parametrize(
    "body, expected",
    [
        ("<decoded_as>sshd</decoded_as>", {"term": {"decoder.name": "sshd"}}),
        (
            "<program_name>sshd</program_name>",
            {
                "wildcard": {
                    "predecoder.program_name": {
                        "value": "*sshd*",
                        "case_insensitive": False,
                    }
                }
            },
        ),
        (
            "<program_name>^sshd</program_name>",
            {"prefix": {"predecoder.program_name": "sshd"}},
        ),
        (
            "<location>^/var/log/auth.log$</location>",
            {"term": {"location": "/var/log/auth.log"}},
        ),
        (
            "<user>root$</user>",
            {
                "wildcard": {
                    "data.dstuser": {"value": "*root", "case_insensitive": False}
                }
            },
        ),
        ("<srcip>10.0.0.1</srcip>", {"term": {"data.srcip": "10.0.0.1"}}),
        ("<dstip>::1</dstip>", {"term": {"data.dstip": "::1"}}),
        (
            '<field name="win.eventdata.image">^C:</field>',
            {"prefix": {"data.win.eventdata.image": "C:"}},
        ),
        (
            "<id>4625|4624</id>",
            {
                "bool": {
                    "should": [
                        {
                            "wildcard": {
                                "data.id": {
                                    "value": "*4625*",
                                    "case_insensitive": False,
                                }
                            }
                        },
                        {
                            "wildcard": {
                                "data.id": {
                                    "value": "*4624*",
                                    "case_insensitive": False,
                                }
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            },
        ),
        (
            "<url>a*b?</url>",
            {
                "wildcard": {
                    "data.url": {"value": "*a\\*b\\?*", "case_insensitive": False}
                }
            },
        ),
    ],
)
def test_a_supported_condition_becomes_its_archive_query(body, expected):
    assert _filters(body) == [expected]


def test_conditions_are_anded_and_inert_elements_are_ignored():
    got = _filters(
        "<decoded_as>sshd</decoded_as><group>ssh,</group>"
        '<field name="action">fail</field><mitre><id>T1110</id></mitre>'
    )
    assert [list(f)[0] for f in got] == ["term", "wildcard"]


@pytest.mark.parametrize(
    "body, attrs, fragment",
    [
        ("<if_sid>5700</if_sid>", "", "<if_sid> depends on another rule"),
        ("<if_group>sshd</if_group>", "", "<if_group> depends"),
        ("<if_matched_sid>5716</if_matched_sid>", "", "<if_matched_sid> depends"),
        (
            "<same_source_ip /><decoded_as>x</decoded_as>",
            "",
            "<same_source_ip> depends",
        ),
        ("<different_srcip />", "", "<different_srcip> depends"),
        (
            "<decoded_as>x</decoded_as>",
            ' frequency="8" timeframe="120"',
            "frequency needs an aggregation",
        ),
        (
            "<decoded_as>x</decoded_as>",
            ' timeframe="120"',
            "timeframe needs an aggregation",
        ),
        ("<decoded_as>x</decoded_as>", ' ignore="60"', "ignore suppresses"),
        ("<decoded_as>x</decoded_as>", ' overwrite="yes"', "overwrite inherits"),
        ("<match>Failed password</match>", "", "<match> on full_log isn't exact"),
        ("<regex>fail\\d+</regex>", "", "<regex> on full_log isn't exact"),
        ('<regex type="pcre2">a</regex>', "", "<regex> on full_log isn't exact"),
        ('<list field="srcip">lists/bad</list>', "", "<list> lookups"),
        ("<hostname>web1</hostname>", "", "<hostname> is the syslog host"),
        ('<field name="x" negate="yes">a</field>', "", "negate on <field>"),
        ("<srcip>!10.0.0.1</srcip>", "", "negation isn't reproduced"),
        ("<srcip>10.0.0.0/8</srcip>", "", "CIDR"),
        ("<srcip>not-an-ip</srcip>", "", "is not one address"),
        ("<program_name>!sshd</program_name>", "", "negation isn't reproduced"),
        ("<program_name>a\\.b</program_name>", "", "an escape in a value"),
        ("<program_name>a^b</program_name>", "", "an anchor inside a value"),
        ("<program_name>a||b</program_name>", "", "an anchor inside a value"),
        ('<field name="x" type="pcre2">a</field>', "", "a pcre2 <field>"),
        ('<field name="bad name">a</field>', "", "isn't translated"),
        ("<srcgeoip>US</srcgeoip>", "", "<srcgeoip> isn't translated"),
        ("<decoded_as></decoded_as>", "", "<decoded_as> is empty"),
        ("", "", "no condition on a decoded field"),
    ],
)
def test_what_cannot_be_reproduced_is_refused_with_its_reason(body, attrs, fragment):
    got = translate(_rule(body, attrs))
    assert got.query is None
    assert any(fragment in r for r in got.reasons), got.reasons
    assert got.why.startswith("can't validate: ")


def test_every_reason_is_reported_once():
    got = translate(_rule("<if_sid>1</if_sid><if_sid>2</if_sid><match>a</match>"))
    assert got.reasons == [
        "<if_sid> depends on another rule having matched",
        "<match> on full_log isn't exact",
    ]


def test_text_that_is_not_one_rule_is_refused():
    assert "not well-formed" in translate("<rule").why


# The run -----------------------------------------------------------------------


class _Service:
    def __init__(self, names=("wazuh-archives-4.x-2026.11.07",), events=True, total=3):
        self.names, self.events, self.total = names, events, total
        self.searched, self.closed = [], False

    async def indices_matching(self, index):
        return list(self.names)

    async def any_event_since(self, index, field, days):
        return self.events

    async def dsl_search(self, index, query, window, size):
        self.searched.append((index, query, window))
        return self.total, [{"_source": {"decoder": {"name": "sshd"}}}] * min(
            self.total, size
        )

    async def close(self):
        self.closed = True


def _copy(rule_id, xml, stage="shadow"):
    return rollout.Copy(
        rule_id=rule_id,
        version=1,
        name=f"rule {rule_id}",
        language="wazuh",
        stage=stage,
        stage_at=NOW - timedelta(days=8),
        checks={"lint": {"result": "pass"}},
        rule={},
        key=str(100000 + rule_id),
        toml=xml,
    )


GOOD = _rule("<decoded_as>sshd</decoded_as>")
CHAIN = _rule("<if_sid>5700</if_sid><decoded_as>sshd</decoded_as>")


@pytest.fixture
def run(monkeypatch):
    """_run_wazuh_copies with its service and writes replaced; returns what it wrote."""
    wrote = {"runs": {}, "unrunnable": [], "sees": {}}

    def go(copies, service, spent=True, enabled=True):
        monkeypatch.setattr(
            rollout.wazuh_sees,
            "indexer",
            lambda: (
                (None, {"max_searches_per_hour": 30})
                if enabled
                else (
                    "elastic-siem is not enabled, so the Wazuh indexer is not read",
                    {},
                )
            ),
        )
        monkeypatch.setattr(rollout.kibana, "service_of", lambda config: service)
        monkeypatch.setattr(rollout.search_budget, "spend", lambda *a, **k: spent)
        monkeypatch.setattr(rollout.exceptions, "active", lambda ids: {})
        monkeypatch.setattr(rollout.search_budget, "last_spent", lambda *a, **k: {})
        monkeypatch.setattr(
            rollout,
            "_write_unrunnable",
            lambda blocked, now: wrote["unrunnable"].extend(blocked) or len(blocked),
        )
        monkeypatch.setattr(
            rollout,
            "_write_sees",
            lambda rows, now: wrote["sees"].update(
                {c.rule_id: (r, w) for c, r, w in rows}
            )
            or len(rows),
        )
        monkeypatch.setattr(
            rollout,
            "_write_runs",
            lambda cs, results, now, excepted=None: wrote["runs"].update(results)
            or len(results),
        )
        report = rollout.RolloutReport()
        rollout._run_wazuh_copies(copies, NOW, report)
        return report

    go.wrote = wrote
    return go


def test_a_translatable_rule_is_searched_in_the_archive_and_its_hits_counted(run):
    service = _Service(total=4)
    report = run([_copy(1, GOOD)], service)
    assert report.searched == 2  # the archive's presence, then the rule
    ((index, query, window),) = service.searched
    assert index == "wazuh-archives-*"
    assert query == {"bool": {"filter": [{"term": {"decoder.name": "sshd"}}]}}
    assert window["@timestamp"]["lt"] == (NOW - rollout.LAG).isoformat()
    total, docs, error = run.wrote["runs"][1]
    assert (total, len(docs), error) == (4, 4, None)
    assert run.wrote["sees"][1][0] == "pass"
    assert service.closed
    # The same structure the Elastic runs leave: a pass tally and the day's hits.
    checks = rollout._ran({}, "shadow", NOW, run.wrote["runs"][1], NOW)
    assert checks["runs"]["days"]["2026-11-07"] == {"pass": 1, "fail": 0}
    assert checks["shadow"]["days"]["2026-11-07"] == 4


def test_an_untranslatable_rule_is_reported_not_run_and_not_validated(run):
    service = _Service()
    run([_copy(1, GOOD), _copy(2, CHAIN)], service)
    assert [c.rule_id for c, _ in run.wrote["unrunnable"]] == [2]
    assert "<if_sid> depends on another rule" in run.wrote["unrunnable"][0][1]
    assert 2 not in run.wrote["runs"]
    assert run.wrote["sees"][2][0] == "unknown"
    assert run.wrote["sees"][1][0] == "pass"


@pytest.mark.parametrize(
    "service",
    [_Service(names=()), _Service(events=False)],
    ids=["no index", "empty"],
)
def test_archives_off_is_cant_validate_and_never_validates(run, service):
    run([_copy(1, GOOD)], service)
    ((copy, why),) = run.wrote["unrunnable"]
    assert why == wazuh_shadow.ARCHIVES_OFF
    assert why.startswith("can't validate: archives off")
    assert run.wrote["runs"] == {} and service.searched == []
    assert run.wrote["sees"][1] == ("unknown", why)


def test_an_unreadable_indexer_leaves_the_rule_unvalidated(run):
    report = run([_copy(1, GOOD)], _Service(), enabled=False)
    assert (
        "can't validate: elastic-siem is not enabled" in run.wrote["unrunnable"][0][1]
    )
    assert report.skipped


def test_a_spent_budget_defers_and_writes_nothing(run):
    report = run([_copy(1, GOOD)], _Service(), spent=False)
    assert report.deferred == 1
    assert run.wrote == {"runs": {}, "unrunnable": [], "sees": {}}


def test_a_live_wazuh_copy_is_the_managers_to_run(run):
    service = _Service()
    run([_copy(1, GOOD, stage="live")], service)
    assert service.searched == [] and run.wrote["runs"] == {}


def _week_of_passes(copy, hits=1):
    week = rollout._week(NOW)
    copy.checks = {
        "lint": {"result": "pass"},
        "runs": {"days": {d: {"pass": 3, "fail": 0} for d in week}},
        "shadow": {"days": {d: hits for d in week}},
        "sees": {"result": "pass"},
    }
    return copy


def test_a_validated_week_promotes_and_an_unvalidated_one_does_not():
    ok = _week_of_passes(_copy(1, GOOD))
    assert rollout._due(None, ok, NOW, 0)[0] == "live"
    # Archives off: Sees is unknown and its runs stopped adding passes.
    ok.checks["sees"] = {"result": "unknown"}
    assert rollout._due(None, ok, NOW, 0) is None
    gap = _week_of_passes(_copy(2, GOOD))
    gap.checks["runs"]["days"][NOW.date().isoformat()] = {"pass": 0, "fail": 0}
    assert rollout._due(None, gap, NOW, 0) is None


def test_a_loud_rule_does_not_promote_at_the_hit_ceiling():
    loud = _week_of_passes(_copy(1, GOOD), hits=rollout.MAX_HITS_PER_DAY + 1)
    assert rollout._due(None, loud, NOW, 0) is None


def test_the_overlay_deploy_list_leaves_out_a_wazuh_copy(monkeypatch):
    monkeypatch.setattr(
        rollout, "_copies", lambda: [_copy(1, GOOD, "live"), _copy(2, "", "live")]
    )
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {})
    assert rollout.overlay_only() == []
