"""Record is one merged read, newest first, with audit rows unchained."""

from datetime import datetime, timezone

from core.cases.case_record import merge_record
from core.storage.models import CaseAuditLog


def _audit(audit_id: int, at: datetime, summary: str) -> CaseAuditLog:
    return CaseAuditLog(
        audit_id=audit_id,
        entity_type="case",
        entity_id="case-1",
        action="update",
        change_summary=summary,
        changed_by="ada",
        timestamp=at,
    )


def test_merge_is_newest_first_and_only_run_rows_are_chained():
    events = [
        {
            "seq": 1,
            "ts": "2026-06-01T00:00:00+00:00",
            "kind": "recall",
            "payload": {},
        },
        {
            "seq": 2,
            "ts": "2026-06-03T00:00:00+00:00",
            "kind": "decision",
            "payload": {"action": "EXAMINE", "rationale": "look"},
        },
    ]
    audits = [
        _audit(7, datetime(2026, 6, 2, tzinfo=timezone.utc), "status changed"),
    ]
    rows = merge_record(events, audits)
    assert [row["id"] for row in rows] == ["run:2", "audit:7", "run:1"]
    assert rows[0]["chained"] is True
    assert rows[0]["text"] == "EXAMINE — look"
    assert rows[1]["chained"] is False
    assert rows[1]["kind"] == "case_audit_logs"
    assert rows[2]["text"] == "Opening recall"


def test_rows_that_share_a_timestamp_stay_newest_seq_first():
    events = [
        {
            "seq": 1,
            "ts": "2026-06-03T00:00:00+00:00",
            "kind": "decision",
            "payload": {"action": "EXAMINE"},
        },
        {
            "seq": 2,
            "ts": "2026-06-03T00:00:00+00:00",
            "kind": "finding",
            "payload": {"agent_id": "lead", "answer": "benign"},
        },
    ]
    rows = merge_record(events, [])
    assert [row["kind"] for row in rows] == ["finding", "decision"]
    assert "_seq" not in rows[0]


def test_activities_are_unchained_rows_sorted_despite_mixed_timestamps():
    events = [
        # The agent writes milliseconds and `Z`.
        {"seq": 1, "ts": "2026-06-02T00:00:00.471Z", "kind": "recall", "payload": {}},
        {"seq": 2, "ts": "2026-06-03T00:00:00.000Z", "kind": "terminal", "payload": {}},
    ]
    audits = [_audit(7, datetime(2026, 6, 2, 12, tzinfo=timezone.utc), "status")]
    activities = [
        # Half a millisecond after run:1; as strings `.471500+00:00` < `.471Z`.
        {
            "timestamp": "2026-06-02T00:00:00.471500Z",
            "activity_type": "agent_run_report",
            "description": "Run concluded",
        },
        "not a dict",
        {"activity_type": "no_timestamp", "description": "skipped"},
        {"timestamp": "not a time", "activity_type": "garbled"},
        {"timestamp": "2026-06-04T00:00:00Z", "activity_type": "resolution"},
    ]
    rows = merge_record(events, audits, activities)
    assert [row["id"] for row in rows] == [
        "activity:4",
        "run:2",
        "audit:7",
        "activity:0",
        "run:1",
    ]
    assert rows[3] == {
        "id": "activity:0",
        "at": "2026-06-02T00:00:00.471500+00:00",
        "kind": "agent_run_report",
        "source": "case_activities",
        "chained": False,
        "text": "Run concluded",
    }
    # No description: the activity type stands in.
    assert rows[0]["text"] == "resolution"
    assert rows[0]["chained"] is False
