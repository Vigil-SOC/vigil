"""The bare-hostname lint names one machine a rule is keyed to, and nothing
else: internet domains, wildcard suffixes, computed ES|QL columns and the
client-sent Workstation names of logon events pass. A rule that only leaves one
host out by name passes with a warning to move that host to an exception."""

import pytest

from core.detections.inventory import sync
from core.detections.lint import lint_query, lint_sigma


def _sigma(selection: str, condition: str = "sel", extra: str = "") -> str:
    return (
        "title: t\nlogsource: {product: windows, service: security}\n"
        f"detection:\n  sel: {selection}\n{extra}  condition: {condition}\n"
    )


@pytest.mark.parametrize(
    "language, query",
    [
        # A wildcard environment suffix on a DNS name: "any internal name".
        ("eql", 'network where dns.question.name : "*.local"'),
        ("eql", 'network where not dns.question.name : ("*.local")'),
        # A column the query computes holds what its EVAL put there.
        (
            "esql",
            "FROM logs-* | EVAL Esql.dest_host = url.domain"
            ' | WHERE Esql.dest_host == "evil-c2"',
        ),
        (
            "esql",
            "FROM logs-* | EVAL Esql.dest_host = url.domain"
            ' | WHERE Esql.dest_host NOT IN ("mcr.microsoft.com", "github.com")',
        ),
        # Internet domains on CIM dest fields are IOCs, not machines.
        ("splunk", "index=proxy dest=pastebin.com"),
        (
            "splunk",
            '| tstats count from datamodel=Web where Web.dest="pastebin.com" by Web.src',
        ),
    ],
)
def test_names_that_are_not_one_machine_pass(language, query):
    report = lint_query(query, language)
    assert report["passed"], report["findings"]
    assert report["findings"] == []


@pytest.mark.parametrize(
    "selection",
    [
        "{EventID: 4776, Workstation: RULER}",
        "{EventID: 4625, WorkstationName: [Rdesktop, Remmina, Freerdp]}",
    ],
)
def test_attack_tool_workstation_names_pass(selection):
    report = lint_sigma(rule_yaml=_sigma(selection))
    assert report["passed"], report["findings"]


@pytest.mark.parametrize(
    "language, query, host",
    [
        ("splunk", "index=win NOT host=dc01 EventCode=4688", "dc01"),
        ("splunk", "index=win host!=dc01", "dc01"),
        (
            "eql",
            'process where host.name != "lab-01-excluded" and process.name == "x.exe"',
            "lab-01-excluded",
        ),
        ("eql", 'process where not host.name in ("dc01", "dc02")', "dc01"),
        ("esql", 'FROM logs-* | WHERE NOT host.name == "dc01"', "dc01"),
        ("lucene", "event.code:4688 AND -host.name:dc01", "dc01"),
    ],
)
def test_one_host_left_out_by_name_only_warns(language, query, host):
    report = lint_query(query, language)
    assert report["passed"]
    assert report["findings"] == []
    assert host in [w["value"] for w in report["warnings"]]
    assert "exception" in report["warning_guidance"]


def test_a_sigma_not_filter_on_one_host_only_warns():
    report = lint_sigma(
        rule_yaml=_sigma(
            "{Image|endswith: '\\\\cmd.exe'}",
            condition="sel and not filter_host",
            extra="  filter_host: {host.name: WS-FIN-07}\n",
        )
    )
    assert report["passed"]
    assert [w["value"] for w in report["warnings"]] == ["WS-FIN-07"]


@pytest.mark.parametrize(
    "language, query, host",
    [
        ("splunk", "host=dc01", "dc01"),
        ("eql", 'process where host.name == "WS-FIN-07"', "WS-FIN-07"),
        ("splunk", "index=fw dvc=fw01 action=blocked", "fw01"),
        ("splunk", "index=win host IN (dc01, dc02)", "dc01"),
    ],
)
def test_a_rule_keyed_to_one_machine_still_fails(language, query, host):
    report = lint_query(query, language)
    assert not report["passed"]
    assert host in [f["value"] for f in report["findings"]]
    assert report["warnings"] == []


def test_a_host_kept_in_still_fails_when_another_is_left_out():
    report = lint_query("index=win NOT host=dc01 host=dc02", "splunk")
    assert [f["value"] for f in report["findings"]] == ["dc02"]
    assert [w["value"] for w in report["warnings"]] == ["dc01"]


def test_an_environment_host_left_out_still_fails():
    # Only a bare host left out warns; .corp/.local names fail as before, even under not.
    report = lint_query('process where host.name != "dc01.corp.local"', "eql")
    assert not report["passed"]


def test_a_synced_copy_keeps_its_warnings(monkeypatch):
    report = lint_query("index=win host!=dc01", "splunk")
    monkeypatch.setattr(sync, "lint_record", lambda record: report)
    lint = sync._lint(object(), "2026-11-07T22:00:00+00:00")
    assert lint["result"] == "pass"
    assert [w["value"] for w in lint["warnings"]] == ["dc01"]
