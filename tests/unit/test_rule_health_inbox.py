"""The rule-health inbox behind the console's bell: each person's read state,
kept with their preferences so it survives a reload; the summary when many
flags are unread; the routes; and Rules filtered to the rules an entry names."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.detections.inventory import catalog, health_inbox
from services.api.routers import detection_rules as routes


def _flag(change_id, rule_id, check="runs", since="2026-11-06T10:00:00+00:00"):
    return {
        "change_id": change_id,
        "rule_id": rule_id,
        "name": f"Rule {rule_id}",
        "tool": "elastic",
        "status": "active",
        "check": check,
        "old": "pass",
        "new": "fail",
        "version": 1,
        "reason": "fail: something",
        "since": since,
    }


@pytest.fixture
def store(monkeypatch):
    """Open flags and the preferences table, in memory."""
    state = {"flags": [], "prefs": {}}
    monkeypatch.setattr(catalog, "open_flags", lambda: list(state["flags"]))
    monkeypatch.setattr(catalog, "open_reverts", lambda: list(state.get("reverts", [])))

    def read_ids(user_id):
        return health_inbox._ids(state["prefs"].get(user_id, {}))

    def update_read(user_id, update):
        prefs = dict(state["prefs"].get(user_id, {}))
        read = update(health_inbox._ids(prefs))
        prefs[health_inbox.READ_KEY] = sorted(read)
        state["prefs"][user_id] = prefs
        return read

    monkeypatch.setattr(health_inbox, "_read_ids", read_ids)
    monkeypatch.setattr(health_inbox, "_update_read", update_read)
    return state


def test_every_open_flag_starts_unread(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2, "sees")]
    box = health_inbox.inbox("ana")
    assert box["unread"] == 2
    assert [e["read"] for e in box["entries"]] == [False, False]
    assert box["summary"] is None


def test_marking_one_read_is_kept_for_that_person_only(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2, "sees")]
    after = health_inbox.mark_read("ana", [11])
    assert after["unread"] == 1
    # A later read (a reload) sees the stored state.
    again = health_inbox.inbox("ana")
    assert [e["change_id"] for e in again["entries"]] == [12]
    assert [e["change_id"] for e in again["earlier"]] == [11]
    assert health_inbox.inbox("ben")["unread"] == 2


def test_mark_all_read(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2, "drift")]
    assert health_inbox.mark_read("ana")["unread"] == 0
    assert health_inbox.inbox("ana")["unread"] == 0


def test_an_id_that_is_no_open_flag_is_ignored(store):
    store["flags"] = [_flag(11, 1)]
    health_inbox.mark_read("ana", [11, 999])
    assert store["prefs"]["ana"][health_inbox.READ_KEY] == [11]


def test_a_cleared_flag_leaves_the_stored_ids(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2)]
    health_inbox.mark_read("ana")
    store["flags"] = [_flag(12, 2)]
    health_inbox.mark_read("ana", [])
    assert store["prefs"]["ana"][health_inbox.READ_KEY] == [12]


def test_a_rule_that_breaks_again_comes_back_unread(store):
    store["flags"] = [_flag(11, 1)]
    health_inbox.mark_read("ana")
    # It cleared, then failed again: a new pass -> fail change row.
    store["flags"] = [_flag(31, 1)]
    assert health_inbox.inbox("ana")["unread"] == 1


def test_other_preferences_survive_a_write(store):
    store["prefs"]["ana"] = {"theme": "dark"}
    store["flags"] = [_flag(11, 1)]
    health_inbox.mark_read("ana")
    assert store["prefs"]["ana"]["theme"] == "dark"


def test_junk_in_the_stored_ids_reads_as_nothing():
    assert health_inbox._ids({health_inbox.READ_KEY: "11"}) == set()
    assert health_inbox._ids({health_inbox.READ_KEY: [11, "12", True, None]}) == {11}
    assert health_inbox._ids(None) == set()


def test_up_to_five_unread_has_no_summary(store):
    store["flags"] = [_flag(i, i) for i in range(1, 6)]
    assert health_inbox.inbox("ana")["summary"] is None


def test_six_unread_get_one_summary_by_check(store):
    checks = ["runs", "runs", "sees", "drift", "silent", "noisy"]
    store["flags"] = [
        _flag(100 + i, i, check, f"2026-11-06T1{9 - i}:00:00+00:00")
        for i, check in enumerate(checks)
    ]
    summary = health_inbox.inbox("ana")["summary"]
    assert summary["count"] == 6
    assert summary["by_check"] == {
        "runs": 2,
        "sees": 1,
        "drift": 1,
        "silent": 1,
        "noisy": 1,
    }
    assert summary["rule_ids"] == [0, 1, 2, 3, 4, 5]
    assert summary["since"] == "2026-11-06T19:00:00+00:00"


def test_summary_counts_unread_only_and_names_each_rule_once(store):
    store["flags"] = [_flag(i, i % 4) for i in range(1, 9)] + [_flag(50, 9)]
    health_inbox.mark_read("ana", [50])
    summary = health_inbox.inbox("ana")["summary"]
    assert summary["count"] == 8
    assert summary["rules"] == 4
    assert sorted(summary["rule_ids"]) == [0, 1, 2, 3]


def test_summary_links_at_most_rules_linked(store, monkeypatch):
    monkeypatch.setattr(health_inbox, "RULES_LINKED", 3)
    store["flags"] = [_flag(i, i) for i in range(1, 9)]
    summary = health_inbox.inbox("ana")["summary"]
    assert summary["rules"] == 8
    assert summary["rule_ids"] == [1, 2, 3]


def test_a_read_entry_leaves_the_list_for_earlier(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2), _flag(13, 3)]
    box = health_inbox.mark_read("ana", [12])
    assert [e["change_id"] for e in box["entries"]] == [11, 13]
    assert all(not e["read"] for e in box["entries"])
    assert [e["change_id"] for e in box["earlier"]] == [12]
    assert box["earlier_total"] == 1


def test_earlier_keeps_the_newest_few_and_counts_them_all(store):
    store["flags"] = [_flag(i, i) for i in range(1, 9)]
    box = health_inbox.mark_read("ana")
    assert box["entries"] == []
    assert [e["change_id"] for e in box["earlier"]] == [1, 2, 3, 4, 5]
    assert box["earlier_total"] == 8
    assert box["total"] == 8


def test_past_five_unread_the_summary_replaces_the_rows(store):
    store["flags"] = [_flag(i, i) for i in range(1, 8)]
    box = health_inbox.inbox("ana")
    assert box["summary"]["count"] == 7
    assert box["entries"] == []
    assert box["unread"] == 7


def test_five_unread_list_one_row_each(store):
    store["flags"] = [_flag(i, i) for i in range(1, 6)]
    box = health_inbox.inbox("ana")
    assert box["summary"] is None
    assert [e["change_id"] for e in box["entries"]] == [1, 2, 3, 4, 5]


# The routes read and write as the signed-in person.


def test_the_inbox_route_reads_as_the_signed_in_person(store):
    store["flags"] = [_flag(11, 1)]
    health_inbox.mark_read("ana")
    ana = SimpleNamespace(user_id="ana")
    ben = SimpleNamespace(user_id="ben")
    assert routes.get_health_inbox(user=ana)["unread"] == 0
    assert routes.get_health_inbox(user=ben)["unread"] == 1


def test_the_read_route_marks_ids_or_all(store):
    store["flags"] = [_flag(11, 1), _flag(12, 2)]
    ana = SimpleNamespace(user_id="ana")
    body = routes.HealthReadRequest(change_ids=[12])
    assert routes.read_health_inbox(body, user=ana)["unread"] == 1
    body = routes.HealthReadRequest(all=True)
    assert routes.read_health_inbox(body, user=ana)["unread"] == 0


@pytest.mark.parametrize("body", [{}, {"change_ids": [1], "all": True}])
def test_the_read_route_wants_ids_or_all_not_both(store, body):
    with pytest.raises(HTTPException) as err:
        routes.read_health_inbox(
            routes.HealthReadRequest(**body), user=SimpleNamespace(user_id="ana")
        )
    assert err.value.status_code == 422


# Rules filtered to the rules an entry names.


def _row(rule_id, health="fires", tool="elastic", name=None):
    row = {
        "rule_id": rule_id,
        "name": name or f"Rule {rule_id}",
        "tool": tool,
        "health": health,
        "worst": health,
        "flags": [],
        "caught": False,
    }
    return (tool, (name or f"rule {rule_id}").lower(), row)


@pytest.fixture
def listing(monkeypatch):
    built = {
        "total": 4,
        "rule_total": 4,
        "mapped": 0,
        "counts": {None: dict.fromkeys(catalog.HEALTH_ORDER, 0)},
        "flag_counts": {None: dict.fromkeys(catalog.FLAGS, 0)},
        "by_state": {state: [] for state in catalog.HEALTH_ORDER},
        "by_siem": {},
        "blind_spots": [],
    }
    built["by_state"]["fails_to_run"] = [
        _row(1, "fails_to_run"),
        _row(2, "fails_to_run"),
    ]
    built["by_state"]["fires"] = [_row(3), _row(4, tool="splunk", name="Shadow copies")]
    monkeypatch.setattr(catalog, "_listing_now", lambda: built)
    return built


def test_listing_narrows_to_the_named_rules(listing):
    out = catalog.health_listing(rule_ids=[2, 4])
    assert [r["rule_id"] for r in out["rules"]] == [2, 4]
    assert out["matched"] == 2


def test_listing_without_rule_ids_lists_all(listing):
    assert catalog.health_listing()["matched"] == 4


def test_named_rules_combine_with_search_and_tool(listing):
    assert catalog.health_listing(rule_ids=[2, 4], q="shadow")["matched"] == 1
    assert catalog.health_listing(rule_ids=[1, 2, 3], tool="splunk")["matched"] == 0


def test_the_rules_route_parses_the_ids(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        catalog, "health_listing", lambda *a, **kw: seen.update(kw) or {"rules": []}
    )
    routes.get_rules(
        health=None,
        tool=None,
        q=None,
        offset=0,
        sort="worst",
        flag=None,
        rules="7,12,7",
    )
    assert seen["rule_ids"] == [7, 12, 7]
    routes.get_rules(
        health=None, tool=None, q=None, offset=0, sort="worst", flag=None, rules=None
    )
    assert seen["rule_ids"] is None
