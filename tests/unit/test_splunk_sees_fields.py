"""Splunk Sees asks a data source for the fields a saved search's first clause
requires: a search whose source gets events but none of them holds such a field
fails with ``missing_fields``, which the Rules page shows as Missing field."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog
from core.detections.inventory import splunk_sees as ss

URL = "https://splunk:8089"
SOURCE = ("raw", ["wineventlog"], ["XmlWinEventLog"], None)
KEY = "splunk:fields:index=wineventlog sourcetype=XmlWinEventLog"
PASS = {"result": "pass", "reason": None, "data_source": ss._source(SOURCE)}


# --- the fields a search requires -----------------------------------------------------


@pytest.mark.parametrize(
    "search, fields",
    [
        ("index=win EventCode=4688 CommandLine=*whoami*", ["EventCode", "CommandLine"]),
        ("search index=win EventCode!=4624 Count>5", ["EventCode", "Count"]),
        ('index=win Image="C:\\\\Windows\\\\cmd.exe" | stats count', ["Image"]),
        ("index=win EventCode IN (4624, 4625) user=*", ["EventCode", "user"]),
        ("index=win (EventCode=1 Image=*cmd*) host=dc1", ["EventCode", "Image"]),
    ],
)
def test_a_first_clauses_terms_are_the_fields_it_requires(search, fields):
    assert ss.fields_of(search) == fields


@pytest.mark.parametrize(
    "search",
    [
        "index=win CommandLine=* OR ParentCommandLine=*",
        "index=win (CommandLine=* OR ParentCommandLine=*)",
        "index=win NOT CommandLine=* NOT (ParentCommandLine=x)",
        'index=win "CommandLine=whoami" TERM(CommandLine=x)',
        "index=win sourcetype=x source=y host=z _time>1 date_hour=3 tag=proc",
        "| tstats count where index=win CommandLine=* by host",
        "index=win | where CommandLine=x | eval y=1",
    ],
)
def test_or_negation_text_defaults_and_later_commands_are_not_required(search):
    assert ss.fields_of(search) == []


def test_splunks_or_binds_before_and():
    # Splunk reads "a b OR c d" as a AND (b OR c) AND d.
    assert ss.fields_of("index=win A=1 B=2 OR C=3 D=4") == ["A", "D"]


def test_a_named_non_internal_index_is_asked():
    assert ss._fields_key(SOURCE) == KEY


@pytest.mark.parametrize(
    "need",
    [
        ("raw", ["*"], [], None),
        ("raw", ["_internal"], ["splunkd"], None),
        ("raw", ["main"], [], "no index named: judged against gray's default"),
        ("dm", "Endpoint", "Processes", None),
    ],
)
def test_any_index_internal_defaults_and_data_models_are_not_asked(need):
    assert ss._fields_key(need) is None


# --- the verdict ----------------------------------------------------------------------


def test_a_field_no_event_holds_fails_as_missing_field():
    out = ss._with_fields(
        PASS, SOURCE, ["EventCode", "CmdLine"], {"EventCode": True, "CmdLine": False}
    )
    assert out["result"] == "fail" and out["missing_fields"] == ["CmdLine"]
    assert out["reason"] == (
        "fail: CmdLine in no event of index=wineventlog sourcetype=XmlWinEventLog in 7d"
    )
    assert out["data_source"] == PASS["data_source"]


def test_every_field_held_or_unsure_keeps_the_pass():
    assert ss._with_fields(PASS, SOURCE, ["A", "B"], {"A": True, "B": None}) == PASS


def test_no_answer_yet_leaves_the_rule():
    assert ss._with_fields(PASS, SOURCE, ["A"], None) is None


def test_a_fail_no_fields_or_an_unasked_source_is_kept():
    fail = {"result": "fail", "reason": "fail: no events", "data_source": None}
    assert ss._with_fields(fail, SOURCE, ["A"], {"A": False}) == fail
    assert ss._with_fields(PASS, SOURCE, [], None) == PASS
    star = ("raw", ["*"], [], None)
    assert ss._with_fields(PASS, star, ["A"], None) == PASS


# --- the search -----------------------------------------------------------------------


class _Service:
    """Answers the raw tstats and the field searches; nothing else is asked."""

    server_url = URL

    def __init__(self, pairs=(), counts=None, warnings=(), raises=None):
        self.pairs, self.counts = list(pairs), counts or {}
        self.warnings, self.raises = list(warnings), raises
        self.asked = []

    def search(self, query, earliest_time, max_count):
        self.asked.append(query)
        if query.startswith("| tstats"):
            rows = [{"index": i, "sourcetype": s, "count": "9"} for i, s in self.pairs]
            return SimpleNamespace(results=rows, warnings=[])
        if self.raises:
            raise RuntimeError(self.raises)
        names = [part.split('"')[1] for part in query.split("count(")[1:]]
        row = {f"f{i}": str(self.counts.get(n, 0)) for i, n in enumerate(names)}
        return SimpleNamespace(results=[row], warnings=self.warnings)

    def can_search_index(self, index):
        return True


@pytest.fixture
def budget(monkeypatch):
    ss.held.clear()
    ss.failed.clear()
    state = {"last": {}, "spend": True, "spent": []}
    monkeypatch.setattr(ss.search_budget, "last_spent", lambda i, keys: state["last"])

    def spend(i, per_hour, key):
        state["spent"].append(key)
        return state["spend"]

    monkeypatch.setattr(ss.search_budget, "spend", spend)
    yield state
    ss.held.clear()
    ss.failed.clear()


def test_the_field_search_counts_each_field_over_the_source():
    service = _Service(counts={"EventCode": 4})
    out = ss._field_search(service, SOURCE, ["EventCode", "CmdLine"])
    assert out == {"EventCode": True, "CmdLine": False}
    (query,) = service.asked
    assert query == (
        'search index="wineventlog" sourcetype="XmlWinEventLog" '
        '| stats count("EventCode") AS f0, count("CmdLine") AS f1'
    )


def test_two_indexes_are_ored():
    service = _Service()
    ss._field_search(service, ("raw", ["a", "b"], [], None), ["X"])
    assert service.asked[0].startswith('search (index="a" OR index="b") | stats')


def test_splunks_warnings_leave_an_absent_field_unsure():
    service = _Service(counts={"A": 1}, warnings=["Search results might be incomplete"])
    assert ss._field_search(service, SOURCE, ["A", "B"]) == {"A": True, "B": None}


def _ask(service, names):
    report = ss.SplunkSeesReport()
    out = ss._field_answers(service, URL, 30, {KEY: (SOURCE, names)}, report)
    return out, report


def test_one_search_per_source_is_held_until_a_new_field_is_asked(budget):
    service = _Service(counts={"A": 2})
    out, report = _ask(service, ["A"])
    assert out == {KEY: {"A": True}} and budget["spent"] == [KEY]
    again, report = _ask(service, ["A"])
    assert again == out and report.searched == 0 and len(service.asked) == 1
    # Searched within 24 h, but this process's answer lacks B: asked again.
    budget["last"] = {KEY: datetime.now(timezone.utc)}
    out, _ = _ask(service, ["A", "B"])
    assert out == {KEY: {"A": True, "B": False}} and len(service.asked) == 2


def test_a_spent_budget_defers(budget):
    budget["spend"] = False
    out, report = _ask(_Service(), ["A"])
    assert out == {} and report.deferred == 1


def test_another_processes_recent_search_leaves_the_rules(budget):
    budget["last"] = {KEY: datetime.now(timezone.utc) - timedelta(hours=2)}
    service = _Service()
    out, _ = _ask(service, ["A"])
    assert out == {} and service.asked == [] and budget["spent"] == []


def test_a_failed_search_answers_nothing_and_is_tried_again(budget):
    out, _ = _ask(_Service(raises="timed out"), ["A"])
    assert out == {} and (URL, KEY) in ss.failed
    budget["last"] = {KEY: datetime.now(timezone.utc)}
    service = _Service(counts={"A": 1})
    out, _ = _ask(service, ["A"])
    assert out == {KEY: {"A": True}} and len(service.asked) == 1


# --- the check ------------------------------------------------------------------------


def _rule(name, search):
    return ((name, 1), {"search": search})


def test_the_check_fails_only_the_search_reading_a_missing_field(budget):
    service = _Service(
        pairs=[("wineventlog", "XmlWinEventLog")], counts={"EventCode": 7}
    )
    rules = [
        _rule("good", "index=wineventlog sourcetype=XmlWinEventLog EventCode=4688"),
        _rule("blind", "index=wineventlog sourcetype=XmlWinEventLog CmdLine=*x*"),
        _rule(
            "either",
            "index=wineventlog sourcetype=XmlWinEventLog (CmdLine=* OR EventCode=1)",
        ),
        _rule("silent", "index=nothing EventCode=1"),
    ]
    report = ss.SplunkSeesReport()
    out = ss._check(service, rules, URL, 30, report)
    assert out[("good", 1)]["result"] == "pass"
    assert out[("either", 1)]["result"] == "pass"
    assert out[("blind", 1)]["missing_fields"] == ["CmdLine"]
    assert out[("silent", 1)]["reason"] == "fail: no events in index=nothing in 7d"
    # One tstats, one field search for the source with events; none for the silent one.
    assert report.searched == 2 and budget["spent"] == [ss.RAW, KEY]


def test_the_check_leaves_a_pass_waiting_on_a_deferred_field_search(
    budget, monkeypatch
):
    service = _Service(pairs=[("wineventlog", "XmlWinEventLog")])
    monkeypatch.setattr(
        ss.search_budget,
        "spend",
        lambda i, per_hour, key: key == ss.RAW,
    )
    rules = [
        _rule("blind", "index=wineventlog sourcetype=XmlWinEventLog CmdLine=*x*"),
        _rule("plain", "index=wineventlog sourcetype=XmlWinEventLog"),
    ]
    report = ss.SplunkSeesReport()
    out = ss._check(service, rules, URL, 30, report)
    assert list(out) == [("plain", 1)] and report.left == 1 and report.deferred == 1


# --- the Rules page -------------------------------------------------------------------


def test_the_rules_page_shows_a_splunk_missing_fields_fail_as_missing_field():
    sees = ss._with_fields(PASS, SOURCE, ["CmdLine"], {"CmdLine": False})
    health, _, raw = catalog._health("active", {"sees": sees}, 0)
    assert health == "missing_field" and raw.startswith("fail: CmdLine in no event")


def test_a_splunk_source_with_no_events_is_still_data_silent():
    sees = {"result": "fail", "reason": "fail: no events in index=x in 7d"}
    assert catalog._health("active", {"sees": sees}, 0)[0] == "data_silent"
