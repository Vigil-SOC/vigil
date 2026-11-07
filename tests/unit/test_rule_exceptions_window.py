"""An overlay rule's exceptions read against its own fires over the same days:
the runner keeps exception matches for KEEP_DAYS, so the panel counts the
rule's fires over those days too, and each exception's share of the hits."""

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


@pytest.fixture
def copy(monkeypatch):
    rule = SimpleNamespace(
        id=5,
        name="Scheduled task from a temp folder",
        source=readers.OVERLAY_SOURCE,
        latest_version=2,
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
    seen = {}

    class _Session:
        def get(self, model, rid):
            return rule if rid == rule.id else None

        def scalar(self, statement):
            return checks

    @contextmanager
    def scope():
        yield _Session()

    def fired(session, name, since):
        seen.update(name=name, since=since)
        return 1480

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
    monkeypatch.setattr(catalog, "fired_since", fired, raising=False)
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    return seen


def test_the_rules_fires_are_counted_over_the_exceptions_days(copy):
    out = exceptions.of_copy(5, now=NOW)
    assert out["window"] == {
        "days": rollout.KEEP_DAYS,
        "since": "2026-10-25",
        "fired": 1480,
        "silenced": 330,
        "hits": 1810,
    }
    assert copy == {
        "name": "Scheduled task from a temp folder",
        "since": datetime(2026, 10, 25),
    }


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
    assert out["window"]["silenced"] == 330


def test_fired_since_counts_findings_that_name_the_rule_from_the_day():
    captured = {}

    class _Session:
        def scalar(self, statement):
            captured["sql"] = str(
                statement.compile(compile_kwargs={"literal_binds": True})
            )
            return 7

    n = catalog.fired_since(
        _Session(), "Scheduled task from a temp folder", datetime(2026, 10, 25)
    )
    assert n == 7
    sql = captured["sql"]
    assert "findings.timestamp >= '2026-10-25 00:00:00'" in sql
    assert "'Scheduled task from a temp folder'" in sql
    assert "rule_name" in sql and "findings.title" in sql
