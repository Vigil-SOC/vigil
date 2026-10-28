"""A failed Splunk query fails the tick, it is not an empty poll (#1256, #1572).

``SplunkService.search`` raises on any error. A failed query makes
``SplunkAdapter.fetch`` raise so the cursor keeps the window, even when a later
fallback would have run. An empty result falls through to the next query (non-ES installs have no notable index).
The last query (the ``notable`` macro, undefined without ES) may fail once every
earlier query ran empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.federation.adapters._base import parse_cursor_since
from core.federation.runner import FederationRunner
from core.integrations.splunk import adapter as splunk_adapter
from core.integrations.splunk.adapter import (
    _QUERIES,
    SplunkAdapter,
    _splunk_event_to_finding,
)
from core.integrations.splunk.client import SearchResult, SplunkSearchError
from core.time import utcnow
from tests.unit._acking_queue import AckingQueue

pytestmark = pytest.mark.unit

_ERR = SplunkSearchError("rejected")
CURSOR = {"last_poll_at": "2026-09-29T02:00:00"}


class _FakeSplunk:
    """Answers each query in order from ``responses``; an exception is raised."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.queries: List[str] = []

    def search(self, **kw) -> Any:
        self.queries.append(kw["query"])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SearchResult(r)


def _fetch(responses: List[Any]):
    adapter = SplunkAdapter()
    svc = _FakeSplunk(responses)
    adapter._service = svc
    return asyncio.run(adapter.fetch(cursor=CURSOR, max_items=10)), svc


def _event(n: int) -> Dict[str, Any]:
    return {"_cd": f"e{n}", "search_name": f"rule {n}", "urgency": "high"}


def test_splunk_event_stores_no_invented_score():
    finding = _splunk_event_to_finding({"_cd": "1", "search_name": "rule"})
    assert finding["anomaly_score"] is None


def test_all_queries_failing_raises():
    with pytest.raises(RuntimeError, match="query failed"):
        _fetch([_ERR, _ERR, _ERR])


def test_all_queries_raise_raises_with_last_error():
    with pytest.raises(RuntimeError, match="index=notable.*read"):
        _fetch([TimeoutError("read"), TimeoutError("read"), OSError("handshake")])


def test_empty_first_query_falls_back_to_next():
    res, svc = _fetch([[], [_event(1)], _ERR])
    assert [f["external_id"] for f in res.findings] == ["e1"]
    assert len(svc.queries) == 2


def test_all_empty_is_a_successful_empty_poll():
    res, svc = _fetch([[], [], []])
    assert res.findings == []
    assert res.cursor["last_poll_at"] > CURSOR["last_poll_at"]
    assert len(svc.queries) == len(_QUERIES)


def test_failed_notable_query_raises_even_when_a_fallback_has_events():
    with pytest.raises(RuntimeError, match=r"index=notable.*permission denied"):
        _fetch([PermissionError("permission denied"), [_event(1)], _ERR])


def test_failed_notable_query_raises_when_the_fallback_is_empty():
    with pytest.raises(RuntimeError, match="index=notable.*rejected"):
        _fetch([_ERR, [], []])


def test_failed_second_query_raises_after_empty_notable():
    with pytest.raises(RuntimeError, match="index=security"):
        _fetch([[], _ERR, [_event(1)]])


def test_failed_macro_query_after_empty_queries_is_a_successful_empty_tick():
    """Non-ES install: no notable index, no alerts, and the macro is undefined."""
    res, svc = _fetch([[], [], _ERR])
    assert res.findings == []
    assert len(svc.queries) == len(_QUERIES)
    assert res.cursor["last_poll_at"] > CURSOR["last_poll_at"]


@pytest.mark.asyncio
async def test_failed_notable_query_keeps_the_cursor_and_records_the_error(
    monkeypatch,
):
    row = {"cursor": dict(CURSOR), "last_error": None, "consecutive_errors": 0}

    def _failure(source_id, error):
        row["last_error"] = error
        row["consecutive_errors"] += 1

    monkeypatch.setattr("core.federation.runner.store.record_failure", _failure)
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("a failed notable query is not a success"),
    )
    adapter = SplunkAdapter()
    adapter.is_configured = lambda: True  # type: ignore[method-assign]
    adapter._service = _FakeSplunk([_ERR, [_event(1)], _ERR])
    runner = FederationRunner(output_queue=AckingQueue())

    for expected in (1, 2):
        await runner._do_one_tick(adapter, {"max_items": 10, "cursor": row["cursor"]})
        assert row["consecutive_errors"] == expected
        adapter._service = _FakeSplunk([_ERR, [_event(1)], _ERR])

    assert "index=notable" in row["last_error"]
    assert row["cursor"] == CURSOR


def test_service_construction_failure_raises_and_is_retried():
    adapter = SplunkAdapter()
    with (
        patch.object(adapter, "is_configured", return_value=True),
        patch(
            "core.integrations.splunk.client.SplunkService",
            side_effect=ValueError("bad url"),
        ),
    ):
        with pytest.raises(ValueError, match="bad url"):
            asyncio.run(adapter.fetch(cursor=CURSOR, max_items=10))
    assert adapter._service is None


def test_not_configured_returns_empty():
    adapter = SplunkAdapter()
    with patch.object(adapter, "is_configured", return_value=False):
        res = asyncio.run(adapter.fetch(cursor=CURSOR, max_items=10))
    assert res.findings == []
    assert "last_poll_at" in res.cursor


# ---------------------------------------------------------------------------
# Full batch (#1571)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 29, 2, 0, 30)  # mid-minute on purpose


def _timed(n: int, when: datetime) -> Dict[str, Any]:
    return {
        "_cd": f"e{n}",
        "_time": when.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds"),
        "urgency": "high",
    }


class _WindowSplunk:
    """Honours an absolute epoch ``earliest_time``, ``sort 0 _time`` and
    ``head N`` the way Splunk does, over a fixed set of events. Like a Splunk
    event search, it selects ``earliest_time`` at whole seconds (#2909)."""

    def __init__(self, events: List[Dict[str, Any]]):
        self.events = events
        self.earliest: List[str] = []

    def search(self, *, query: str, earliest_time: str, max_count: int, **kw):
        self.earliest.append(earliest_time)
        assert "sort 0 _time" in query
        floor = math.floor(float(earliest_time))
        hits = [
            e
            for e in self.events
            if datetime.fromisoformat(e["_time"]).timestamp() >= floor
        ]
        return SearchResult(sorted(hits, key=lambda e: e["_time"])[:max_count])


def _window_adapter(events: List[Dict[str, Any]]):
    adapter = SplunkAdapter()
    svc = _WindowSplunk(events)
    adapter._service = svc
    return adapter, svc


def test_full_batch_keeps_the_oldest_and_stops_the_cursor_at_the_newest_kept():
    adapter, svc = _window_adapter(
        [_timed(i, T0 + timedelta(minutes=i)) for i in range(1, 6)]
    )

    res = asyncio.run(
        adapter.fetch(cursor={"last_poll_at": T0.isoformat()}, max_items=3)
    )

    assert [f["external_id"] for f in res.findings] == ["e1", "e2", "e3"]
    assert parse_cursor_since(res.cursor) == T0 + timedelta(minutes=3)
    # Absolute epoch of the cursor, not a minute-rounded "-Nm".
    assert float(svc.earliest[0]) == T0.replace(tzinfo=timezone.utc).timestamp()


def test_events_in_one_minute_still_progress_the_cursor():
    """A full batch inside one minute must not re-read the same page forever."""
    events = [_timed(i, T0 + timedelta(seconds=i)) for i in range(1, 8)]
    adapter, _ = _window_adapter(events)

    cursor = {"last_poll_at": T0.isoformat()}
    seen: List[str] = []
    for _ in range(4):
        res = asyncio.run(adapter.fetch(cursor=cursor, max_items=3))
        seen += [f["external_id"] for f in res.findings]
        cursor = res.cursor

    assert set(seen) == {f"e{i}" for i in range(1, 8)}
    # Ticks 1-3 filled the batch; the cursor advanced each time (e3, e6, e7...).
    assert parse_cursor_since(cursor) > T0 + timedelta(seconds=7)


def test_a_second_denser_than_max_items_is_left_on_the_next_fetch(caplog):
    """Splunk filters earliest_time at whole seconds: a 1 ms step would re-read
    the same page every tick until the second is crossed (#2909)."""
    stuck = T0.replace(microsecond=0)
    events = [_timed(i, stuck) for i in range(1, 6)]
    events += [_timed(i, stuck + timedelta(seconds=1)) for i in (6, 7)]
    adapter, svc = _window_adapter(events)
    # Mid-second, as the lab cursor was.
    start = stuck + timedelta(milliseconds=933)

    with caplog.at_level("WARNING"):
        first = asyncio.run(
            adapter.fetch(cursor={"last_poll_at": start.isoformat()}, max_items=3)
        )
    assert [f["external_id"] for f in first.findings] == ["e1", "e2", "e3"]
    assert parse_cursor_since(first.cursor) == start + timedelta(seconds=1)
    assert "beyond this batch are skipped" in caplog.text

    second = asyncio.run(adapter.fetch(cursor=first.cursor, max_items=3))
    assert [f["external_id"] for f in second.findings] == ["e6", "e7"]


def test_the_one_second_step_is_capped_at_now(monkeypatch):
    stuck = T0.replace(microsecond=0)
    now = stuck + timedelta(milliseconds=400)
    monkeypatch.setattr("core.integrations.splunk.adapter.utcnow", lambda: now)
    adapter, _ = _window_adapter([_timed(i, stuck) for i in range(1, 6)])

    res = asyncio.run(
        adapter.fetch(cursor={"last_poll_at": stuck.isoformat()}, max_items=3)
    )
    assert parse_cursor_since(res.cursor) == now


def test_full_batch_with_unreadable_times_warns_and_moves_to_now(caplog):
    events = [{"_cd": f"e{i}", "_time": "garbage"} for i in range(3)]
    adapter = SplunkAdapter()
    adapter._service = _FakeSplunk([events])

    before = utcnow()
    with caplog.at_level("WARNING"):
        res = asyncio.run(adapter.fetch(cursor=CURSOR, max_items=3))

    assert parse_cursor_since(res.cursor) >= before
    assert "no alert carried a readable time" in caplog.text


# --- ATT&CK annotations (#3218) ---------------------------------------------
# Hand-built notables in the documented shapes; nothing is copied from Splunk.


@pytest.fixture(autouse=True)
def _fresh_warnings():
    splunk_adapter._warned_annotation_keys.clear()
    yield
    splunk_adapter._warned_annotation_keys.clear()


def _notable(**fields: Any) -> Dict[str, Any]:
    return {"_cd": "n1", "search_name": "Encoded PowerShell", **fields}


def _mitre(event: Dict[str, Any]) -> Dict[str, Any]:
    return _splunk_event_to_finding(event)["mitre_predictions"]


def _warnings(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# Splunk ES 7.3 docs, "Share data in Splunk Enterprise Security": the
# `annotations` field is a JSON string whose mitre_attack is a string or list.
@pytest.mark.parametrize(
    "annotations",
    [
        json.dumps({"mitre_attack": ["T1059.001", "T1071"]}),
        json.dumps({"mitre_attack": "T1059.001,T1071"}),
        json.dumps({"mitre_attack": "T1059.001, T1071", "nist": ["DE.CM"]}),
    ],
    ids=["object-with-list", "object-with-string", "string"],
)
def test_encoded_annotations_give_techniques_with_unknown_confidence(annotations):
    assert _mitre(_notable(annotations=annotations)) == {
        "T1059.001": None,
        "T1071": None,
    }


# Splunk ES 8.0 / 8.5 docs, "Add annotations to detections": a dotted
# `annotations.mitre_attack` field holding a string, comma list or list.
@pytest.mark.parametrize(
    "value", ["T1059.001,T1071", ["T1059.001", "T1071"], ["T1059.001,T1071"]]
)
def test_dotted_annotation_field(value):
    assert _mitre(_notable(**{"annotations.mitre_attack": value})) == {
        "T1059.001": None,
        "T1071": None,
    }


# Weaker evidence than the above: rests on Splunk's general JSON-extraction
# naming for arrays (SPL2 "spath command"; Splunk Community "Why need curly
# braces at the end of field", "JSON Field Extraction names with curly
# brackets"), not on an ES page showing a notable.
def test_curly_brace_array_field():
    event = _notable(**{"annotations.mitre_attack{}": ["T1059.001", "T1071"]})
    assert _mitre(event) == {"T1059.001": None, "T1071": None}


def test_forms_are_unioned_deduplicated_and_normalised(caplog):
    event = _notable(
        annotations=json.dumps({"mitre_attack": ["t1059", "T1071"]}),
        **{
            "annotations.mitre_attack": " t1059.001 , Execution,,T1071",
            "annotations.mitre_attack{}": ["T1059", "", "T10", "T1059.0011"],
        },
    )
    # Parent and sub-technique both kept as sent; tactic and junk dropped.
    assert _mitre(event) == {"T1059": None, "T1071": None, "T1059.001": None}
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"annotations.mitre_attack": ""},
        {"annotations.mitre_attack{}": []},
        {"annotations": json.dumps({"mitre_attack": []})},
        {"annotations": json.dumps({"cis20": ["CIS 8"], "nist": ["DE.AE"]})},
        {"annotations": ""},
    ],
    ids=["none", "empty-string", "empty-list", "empty-encoded", "other-only", "blank"],
)
def test_nothing_to_read_is_silent(fields, caplog):
    assert _mitre(_notable(**fields)) == {}
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"annotations": "{not json"},
        {"annotations": json.dumps(["T1059"])},
        {"annotations": json.dumps({"mitre_attack": 1059})},
        {"annotations.mitre_attack": 1059},
        {"annotations.mitre_attack{}": {"id": "T1059"}},
        {"annotations.mitre_attack": "Execution"},
        {"annotations.mitre_attack.mitre_technique_id": "T1059"},
    ],
    ids=[
        "bad-json",
        "encoded-list",
        "encoded-number",
        "number",
        "object",
        "tactic-only",
        "subfield-only",
    ],
)
def test_unreadable_annotation_warns_once_per_key_and_still_ingests(fields, caplog):
    first = _splunk_event_to_finding(_notable(**fields))
    second = _splunk_event_to_finding(_notable(_cd="n2", **fields))

    assert first["finding_id"] == "splunk-n1" and first["mitre_predictions"] == {}
    assert second["finding_id"] == "splunk-n2"
    (warning,) = _warnings(caplog)
    assert "Encoded PowerShell" in warning and "annotations" in warning


def test_warning_falls_back_to_rule_name_then_sourcetype(caplog):
    bad = {"annotations": "{not json"}
    _splunk_event_to_finding({"_cd": "a", "rule_name": "Rule A", **bad})
    _splunk_event_to_finding({"_cd": "b", "sourcetype": "edr:alert", **bad})
    _splunk_event_to_finding({"_cd": "c", "sourcetype": "ids:alert", **bad})
    _splunk_event_to_finding({"_cd": "d", "sourcetype": "ids:alert", **bad})

    warnings = _warnings(caplog)
    assert len(warnings) == 3
    assert ["Rule A" in warnings[0], "edr:alert" in warnings[1]] == [True, True]


def test_malformed_notable_does_not_stop_its_batch():
    bad = _notable(_cd="bad", annotations="{not json")
    good = _notable(_cd="good", **{"annotations.mitre_attack": "T1071"})

    res, _ = _fetch([[bad, good]])

    assert [(f["external_id"], f["mitre_predictions"]) for f in res.findings] == [
        ("bad", {}),
        ("good", {"T1071": None}),
    ]
