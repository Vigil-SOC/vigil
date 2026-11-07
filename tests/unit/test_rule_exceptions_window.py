"""An overlay rule's exceptions read against its own fires over the same days:
the runner keeps exception matches for KEEP_DAYS, so the panel counts the
rule's fires over those days too, counted as the Fires column counts them,
and each exception's share of the hits."""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.detections import exceptions
from core.detections.inventory import catalog, readers, rollout

NOW = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)


def _exc(eid, removed=False):
    return {
        "exception_id": eid,
        "match": {"host.name": f"H{eid}"},
        "note": "n",
        "created_by": "d.lee",
        "created_at": None,
        "removed_at": "2026-11-05T00:00:00" if removed else None,
        "removed_by": None,
        "removal_reason": None,
    }


NAME = "Scheduled task from a temp folder"


def _hit(fid, day):
    return {
        "finding_id": fid,
        "title": NAME,
        "severity": "high",
        "timestamp": f"{day}T08:00:00+00:00",
    }


def _listed(rule_id):
    return SimpleNamespace(
        id=rule_id,
        name=NAME,
        language="esql",
        source=readers.OVERLAY_SOURCE,
        status="active",
        techniques=[],
        checks={},
    )


# Two listed copies share the name. Fires traces each finding to one of them;
# the findings only the name matches count on neither.
FIRES = {
    "by_rule": {
        5: [
            _hit("f1", "2026-11-07"),
            _hit("f2", "2026-11-01"),
            _hit("f3", "2026-10-25"),
            # Before the window's first day.
            _hit("f4", "2026-10-24"),
        ],
        6: [_hit("f5", "2026-11-06"), _hit("f6", "2026-11-02")],
    },
    "by_name": {NAME: [_hit(f"n{i}", "2026-11-05") for i in range(40)]},
}


@pytest.fixture
def listing(monkeypatch):
    copies = {
        "rows": [_listed(5), _listed(6)],
        "rule_of": {5: (5,), 6: (6,)},
        "blind_spots": [],
    }
    built = catalog._health_rows(copies, fires=FIRES)
    monkeypatch.setattr(catalog, "_listing_now", lambda: built)
    return built


@pytest.fixture
def copy(monkeypatch, listing):
    rule = SimpleNamespace(
        id=5, name=NAME, source=readers.OVERLAY_SOURCE, latest_version=2
    )
    checks = {
        "exceptions": {
            # The window is the 14 UTC days 2026-10-25..11-07, so 10-24 falls outside it.
            "11": {"days": {"2026-10-24": 10, "2026-11-01": 150, "2026-11-07": 50}},
            "12": {"days": {"2026-11-02": 130}},
            # Counted before the window: outside it, not silenced in it.
            "13": {"days": {"2026-10-23": 900, "2026-11-03": 0}},
        }
    }

    class _Session:
        def get(self, model, rid):
            return rule if rid == rule.id else None

        def scalar(self, statement):
            return checks

    @contextmanager
    def scope():
        yield _Session()

    monkeypatch.setattr(
        exceptions, "get_db_manager", lambda: SimpleNamespace(session_scope=scope)
    )
    monkeypatch.setattr(
        exceptions,
        "listed",
        lambda session, rid, removed=False: [
            _exc(11),
            _exc(12),
            _exc(13, removed=True),
        ],
    )
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())


def test_the_rules_fires_are_counted_over_the_exceptions_days(copy):
    out = exceptions.of_copy(5, now=NOW)
    assert out["window"] == {
        "days": rollout.KEEP_DAYS,
        "since": "2026-10-25",
        "fired": 3,
        "silenced": 330,
        "hits": 333,
    }


def test_two_rules_with_one_name_keep_their_own_fires(listing):
    # Each copy counts the findings traced to it in the window; the 40 that
    # only the shared name matches count on neither, as in the Fires column.
    assert catalog.fired_in_window(5, "2026-10-25") == 3
    assert catalog.fired_in_window(6, "2026-10-25") == 2
    assert catalog.fired_in_window(5, "2026-11-02") == 1
    assert catalog.fired_in_window(99, "2026-10-25") == 0


def test_each_exception_says_what_it_silenced_in_that_window(copy):
    out = exceptions.of_copy(5, now=NOW)
    assert {x["exception_id"]: x["silenced"] for x in out["exceptions"]} == {
        11: 200,
        12: 130,
        13: 0,
    }
    # The per-day counts are still all there for the hover.
    assert out["exceptions"][0]["days"]["2026-10-24"] == 10


def test_the_window_follows_the_runners_keep_days(copy, monkeypatch):
    monkeypatch.setattr(rollout, "KEEP_DAYS", 7)
    out = exceptions.of_copy(5, now=NOW)
    assert out["window"]["since"] == "2026-11-01"
    assert out["window"]["fired"] == 2
    assert out["window"]["silenced"] == 330
