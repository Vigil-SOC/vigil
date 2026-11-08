"""The bell tells everyone when Vigil itself turns a live or shadow rule off (a new
version failing lint, say), with the reason, until the rule's stage moves again;
Vigil's Reverts stay entries as before."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog, health_inbox

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
LINT = "fails lint: keyed to ip 10.20.0.15"


def _row(sid, rule_id, stage, actor="system", reason="", hours_ago=0, status="active"):
    return SimpleNamespace(
        id=sid,
        rule_id=rule_id,
        version=2,
        stage=stage,
        actor=actor,
        reason=reason,
        at=NOW - timedelta(hours=hours_ago),
        name=f"Rule {rule_id}",
        source="repo:overlay",
        status=status,
    )


def _entries(*rows):
    return catalog.vigil_moves(list(rows))


@pytest.mark.parametrize("before", ["live", "shadow"])
def test_vigil_turning_a_rule_off_is_an_entry_with_its_reason(before):
    (entry,) = _entries(
        _row(1, 7, before, hours_ago=30), _row(2, 7, "off", reason=LINT)
    )
    assert entry["check"] == "off"
    assert (entry["old"], entry["new"]) == (before, "off")
    assert entry["reason"] == LINT
    assert (
        entry["change_id"] == -2
        and entry["rule_id"] == 7
        and entry["tool"] == "overlay"
    )


def test_a_rule_that_never_ran_is_no_entry():
    """A new rule whose first version fails lint starts off: nothing stopped running."""
    assert _entries(_row(1, 7, "off", reason=LINT)) == []


def test_the_entry_clears_when_the_stage_moves_again():
    rows = [
        _row(1, 7, "live", hours_ago=30),
        _row(2, 7, "off", reason=LINT, hours_ago=5),
    ]
    assert [e["check"] for e in _entries(*rows)] == ["off"]
    fixed = rows + [_row(3, 7, "shadow", reason="new version")]
    assert _entries(*fixed) == []


def test_the_last_row_is_the_last_in_time_not_by_id():
    """A row written later but stamped earlier is not the rule's current stage."""
    rows = [
        _row(1, 7, "live", hours_ago=30),
        _row(3, 7, "off", reason=LINT, hours_ago=5),
        _row(4, 7, "shadow", reason="late write of an older move", hours_ago=40),
    ]
    (entry,) = _entries(*rows)
    assert (entry["check"], entry["old"], entry["change_id"]) == ("off", "live", -3)
    # And the other way: the newest in time clears it, whatever its id.
    rows = [
        _row(1, 7, "live", hours_ago=30),
        _row(5, 7, "off", reason=LINT, hours_ago=5),
        _row(2, 7, "shadow", reason="new version", hours_ago=1),
    ]
    assert _entries(*rows) == []


def test_a_removed_rule_is_no_entry():
    """Its file or source was removed: the rule is gone, not switched off."""
    rows = [
        _row(1, 7, "live", hours_ago=30),
        _row(2, 7, "off", reason="removed", status="removed"),
    ]
    assert _entries(*rows) == []


def test_reverts_stay_entries_and_both_list_newest_first():
    rows = [
        _row(1, 7, "live", hours_ago=50),
        _row(2, 7, "off", reason=LINT, hours_ago=3),
        _row(3, 8, "live", hours_ago=50),
        _row(4, 8, "shadow", reason="a live day above 20 hits", hours_ago=1),
        _row(5, 9, "live", hours_ago=50),
        _row(6, 9, "shadow", actor="ana", reason="too loud", hours_ago=2),
    ]
    assert [(e["rule_id"], e["check"]) for e in _entries(*rows)] == [
        (8, "revert"),
        (7, "off"),
    ]


def test_everyone_sees_it_and_each_person_reads_it_alone(monkeypatch):
    (off,) = _entries(_row(1, 7, "live", hours_ago=30), _row(2, 7, "off", reason=LINT))
    monkeypatch.setattr(catalog, "open_flags", lambda: [])
    monkeypatch.setattr(catalog, "open_vigil_moves", lambda: [off])
    reads = {}
    monkeypatch.setattr(health_inbox, "_read_ids", lambda u: reads.get(u, set()))

    def update(user_id, fn):
        reads[user_id] = fn(reads.get(user_id, set()))
        return reads[user_id]

    monkeypatch.setattr(health_inbox, "_update_read", update)
    for person in ("ana", "ben"):
        box = health_inbox.inbox(person)
        assert [(e["check"], e["reason"]) for e in box["entries"]] == [("off", LINT)]
        assert box["unread"] == 1
    health_inbox.mark_read("ana", [-2])
    assert health_inbox.inbox("ana")["unread"] == 0
    assert health_inbox.inbox("ben")["unread"] == 1
