"""Wazuh Sees asks the archive for the decoded fields a rule reads: a rule whose
decoder gets events but none of them holds a field it reads fails with
``missing_fields``, which the Rules page shows as Missing field."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.detections.inventory import catalog, sees
from core.detections.inventory import wazuh_sees as ws

URL = "http://indexer:9200"
# The manager's own listing (GET /rules details), as the lab's 4.9.2 lists them.
FAILED_LOGON = {
    "if_sid": "60105",
    "win.system.eventID": {"pattern": "^529$|^4625$"},
    "options": "no_full_log",
}
CMD_FROM_EXPLORER = {
    "if_group": "sysmon_event1",
    "win.eventdata.originalFileName": {"pattern": "(?i)cmd\\.EXE", "type": "pcre2"},
    "win.eventdata.parentImage": {"pattern": "(?i)explorer", "negate": "yes"},
}
SSH_ACCEPTED = {"if_sid": "5700", "match": {"pattern": "^Accepted"}}
SSH_FROM = {
    "if_sid": "5715",
    "srcip": {"pattern": "10.0.0.5"},
    "user": {"pattern": "root"},
}


def _archive(*decoders):
    return {
        "total": 40,
        "failed": [],
        ws.DECODER: {"values": set(decoders), "other": 0},
    }


def _alerts(*rule_ids):
    return {
        "total": 5,
        "failed": [],
        ws.DECODER: {"values": set(), "other": 0},
        ws.RULE: {"values": set(rule_ids), "other": 0},
    }


PASS = {
    "result": "pass",
    "reason": "events decoded as windows_eventchannel in 7d",
    "data_source": "wazuh decoder windows_eventchannel",
}


# --- the fields a rule reads ---------------------------------------------------------


def test_a_rules_field_conditions_are_the_fields_it_reads():
    assert ws.rule_fields(FAILED_LOGON) == ["win.system.eventID"]


def test_a_negated_field_and_non_field_elements_are_not_asked():
    assert ws.rule_fields(CMD_FROM_EXPLORER) == ["win.eventdata.originalFileName"]
    assert ws.rule_fields(SSH_ACCEPTED) == []


def test_static_elements_are_asked_at_their_archive_paths():
    assert ws.rule_fields(SSH_FROM) == ["srcip", "user"]
    q = ws._pair_query("sshd", "user")["bool"]["filter"]
    assert q[0] == {"term": {"decoder.name": "sshd"}}
    assert q[1]["bool"]["should"] == [
        {"exists": {"field": "data.dstuser"}},
        {"exists": {"field": "data.srcuser"}},
    ]
    q = ws._pair_query("windows_eventchannel", "win.system.eventID")["bool"]["filter"]
    assert q[1]["bool"]["should"] == [{"exists": {"field": "data.win.system.eventID"}}]


def test_a_child_reads_its_ancestors_fields_too():
    details = {
        "60103": {"win.system.channel": {"pattern": "^Security$"}},
        "60105": {"if_sid": "60103", "win.system.severityValue": {"pattern": "^AUDIT"}},
        "60122": FAILED_LOGON,
    }
    _, anc = ws._lineage(details)("60122")
    assert ws.lineage_fields("60122", anc, details) == [
        "win.system.eventID",
        "win.system.channel",
        "win.system.severityValue",
    ]


# --- the verdict ----------------------------------------------------------------------


def _with(
    answers, fields=("win.system.eventID",), alerts=None, archive=None, rid="60122"
):
    return ws._with_fields(
        dict(PASS),
        rid,
        {"windows_eventchannel"},
        list(fields),
        archive or _archive("windows_eventchannel"),
        alerts or _alerts(),
        answers,
    )


def test_a_field_no_archived_event_holds_fails_as_missing_field():
    out = _with({("windows_eventchannel", "win.system.eventID"): False})
    assert out["result"] == "fail"
    assert out["missing_fields"] == ["win.system.eventID"]
    assert out["reason"] == (
        "fail: win.system.eventID in no event decoded as windows_eventchannel in 7d"
    )
    assert out["data_source"] == PASS["data_source"]


def test_a_field_the_events_hold_keeps_the_pass():
    assert _with({("windows_eventchannel", "win.system.eventID"): True}) == PASS


def test_only_the_missing_fields_are_named():
    out = _with(
        {
            ("windows_eventchannel", "win.system.eventID"): True,
            ("windows_eventchannel", "win.eventdata.commandLine"): False,
        },
        fields=("win.system.eventID", "win.eventdata.commandLine"),
    )
    assert out["missing_fields"] == ["win.eventdata.commandLine"]


def test_a_field_any_of_its_decoders_holds_is_not_missing():
    out = ws._with_fields(
        dict(PASS),
        "1",
        {"a", "b"},
        ["x"],
        _archive("a", "b"),
        _alerts(),
        {("a", "x"): False, ("b", "x"): True},
    )
    assert out == PASS


def test_a_rule_that_alerted_keeps_its_pass():
    assert _with({}, alerts=_alerts("60122")) == PASS


def test_a_pass_on_alerts_alone_is_not_asked():
    assert _with({}, archive=_archive("sshd")) == PASS


def test_no_answer_yet_leaves_the_rule_as_it_is():
    assert _with({}) is None


def test_a_rule_reading_no_decoded_field_keeps_its_pass():
    assert _with({}, fields=()) == PASS


def test_a_fail_or_unknown_is_never_asked():
    fail = {"result": "fail", "reason": "fail: no events", "data_source": None}
    assert (
        ws._with_fields(fail, "1", {"a"}, ["x"], _archive("a"), _alerts(), {}) == fail
    )


# --- the search -----------------------------------------------------------------------


class _Service:
    def __init__(self, counts, failed=(), raises=None):
        self.counts, self.failed, self.raises = counts, list(failed), raises
        self.asked = []

    async def counts_since(self, index, queries, days):
        self.asked.append((index, queries, days))
        if self.raises:
            raise RuntimeError(self.raises)
        return {
            "total": 40,
            "failed": self.failed,
            "counts": {k: self.counts.get(k, 0) for k in queries},
        }


@pytest.fixture
def budget(monkeypatch):
    ws.held_fields.clear()
    ws.failed.clear()
    state = {"last": None, "spend": True, "spent": []}
    monkeypatch.setattr(
        ws.search_budget,
        "last_spent",
        lambda i, keys: {ws.FIELDS_KEY: state["last"]} if state["last"] else {},
    )

    def spend(i, per_hour, key):
        state["spent"].append(key)
        return state["spend"]

    monkeypatch.setattr(ws.search_budget, "spend", spend)
    yield state
    ws.held_fields.clear()
    ws.failed.clear()


def _ask(service, pairs):
    report = ws.WazuhSeesReport()
    out = asyncio.run(ws._field_answers(service, URL, 30, set(pairs), report))
    return out, report


PAIRS = [("windows_eventchannel", "win.system.eventID"), ("sshd", "srcip")]


def test_one_search_answers_every_pair_and_is_held(budget):
    service = _Service({"0": 0, "1": 12})
    out, report = _ask(service, PAIRS)
    assert budget["spent"] == [ws.FIELDS_KEY] and report.searched == 1
    ((index, queries, days),) = service.asked
    assert index == ws.ARCHIVES and days == ws.DAYS and len(queries) == 2
    assert out == {
        ("sshd", "srcip"): False,
        ("windows_eventchannel", "win.system.eventID"): True,
    }
    again, report = _ask(service, PAIRS)
    assert again == out and report.searched == 0 and len(service.asked) == 1


def test_a_spent_budget_defers_and_answers_nothing(budget):
    budget["spend"] = False
    out, report = _ask(_Service({}), PAIRS)
    assert out == {} and report.deferred == 1


def test_another_processes_recent_search_leaves_the_rules(budget):
    budget["last"] = datetime.now(timezone.utc) - timedelta(hours=2)
    service = _Service({})
    out, _ = _ask(service, PAIRS)
    assert out == {} and service.asked == [] and budget["spent"] == []


def test_failed_shards_prove_no_absence(budget):
    out, _ = _ask(_Service({"1": 3}, failed=["wazuh-archives-x: mapping"]), PAIRS)
    assert out == {("windows_eventchannel", "win.system.eventID"): True}


def test_a_failed_search_answers_nothing_and_is_tried_again(budget):
    out, _ = _ask(_Service({}, raises="timeout"), PAIRS)
    assert out == {} and (URL, ws.FIELDS_KEY) in ws.failed
    budget["last"] = datetime.now(timezone.utc)
    service = _Service({"0": 1, "1": 1})
    out, _ = _ask(service, PAIRS)
    assert len(service.asked) == 1 and all(out.values())


# --- the sweep ------------------------------------------------------------------------


class _SweepService(_Service):
    async def close(self):
        pass


def _sweep(monkeypatch, details, active, counts):
    service = _SweepService(counts)
    written = {}

    async def answers(*_):
        return None, {
            ws.ARCHIVES: _archive("windows_eventchannel"),
            ws.ALERTS: _alerts(),
        }

    monkeypatch.setattr(ws, "_rules", lambda: ([(r, r) for r in active], details))
    monkeypatch.setattr(ws, "indexer", lambda: (None, {"elasticsearch_url": URL}))
    monkeypatch.setattr(ws.kibana, "service_of", lambda config: service)
    monkeypatch.setattr(ws, "_answers", answers)
    monkeypatch.setattr(
        ws.sees, "write_check", lambda kind, results, source: written.update(results)
    )
    report = ws.sweep()
    return report, written, service


def test_the_sweep_fails_a_child_whose_parent_reads_a_missing_field(
    monkeypatch, budget
):
    details = {
        "60000": {"decoded_as": "windows_eventchannel"},
        "60103": {"if_sid": "60000", "win.system.channel": {"pattern": "^Security$"}},
        "60122": {**FAILED_LOGON, "if_sid": "60103"},
    }
    # One search: query 0 is win.system.channel, query 1 win.system.eventID.
    report, written, service = _sweep(
        monkeypatch, details, ["60000", "60122"], {"1": 40}
    )
    assert report.error is None and len(service.asked) == 1
    assert written["60000"] == PASS
    assert written["60122"]["result"] == "fail"
    assert written["60122"]["missing_fields"] == ["win.system.channel"]


def test_the_sweep_leaves_a_pass_waiting_on_a_deferred_field_search(
    monkeypatch, budget
):
    budget["spend"] = False
    details = {
        "60000": {"decoded_as": "windows_eventchannel"},
        "60122": {**FAILED_LOGON, "if_sid": "60000"},
    }
    report, written, _ = _sweep(monkeypatch, details, ["60000", "60122"], {})
    assert written == {"60000": PASS} and report.left == 1 and report.deferred == 1


# --- the Rules page -------------------------------------------------------------------


def test_the_rules_page_shows_a_wazuh_missing_fields_fail_as_missing_field():
    checks = {
        "runs": {"result": "pass"},
        "sees": _with({("windows_eventchannel", "win.system.eventID"): False}),
    }
    health, why, raw = catalog._health("active", checks, 0)
    assert health == "missing_field"
    assert raw.startswith("fail: win.system.eventID in no event decoded as")


def test_a_wazuh_decoder_with_no_events_is_still_data_silent():
    checks = {
        "runs": {"result": "pass"},
        "sees": {"result": "fail", "reason": "fail: no events decoded as sshd in 7d"},
    }
    assert catalog._health("active", checks, 0)[0] == "data_silent"


def test_elastics_mapping_fail_names_its_missing_fields():
    out = sees._verdict(
        "logs-*", "@timestamp", ["process.command_line"], {"host.name"}, ""
    )
    assert out["missing_fields"] == ["process.command_line"]
    assert catalog._health("active", {"sees": out}, 0)[0] == "missing_field"
