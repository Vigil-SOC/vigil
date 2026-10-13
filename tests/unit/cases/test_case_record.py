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


def test_activities_are_unchained_rows_sorted_among_run_and_audit_rows():
    events = [
        {
            "seq": 1,
            "ts": "2026-06-01T10:00:00.000Z",
            "kind": "decision",
            "payload": {"action": "EXAMINE"},
        },
        {
            "seq": 2,
            "ts": "2026-06-01T10:20:00.607Z",
            "kind": "terminal",
            "payload": {"outcome": "completed"},
        },
    ]
    audits = [_audit(7, datetime(2026, 6, 1, 10, 10), "file attached")]
    activities = [
        {
            "timestamp": "2026-06-01T10:05:00.250000Z",
            "activity_type": "finding_added",
            "description": "Hunt run-1 added alert f-1",
            "details": {},
        },
        {
            "timestamp": "2026-06-01T10:20:00.607123Z",
            "activity_type": "agent_run_report",
            "description": "",
            "details": {},
        },
    ]
    rows = merge_record(events, audits, activities)
    assert [row["id"] for row in rows] == [
        "activity:1",
        "run:2",
        "audit:7",
        "activity:0",
        "run:1",
    ]
    report, added = rows[0], rows[3]
    assert report["source"] == added["source"] == "case_activities"
    assert report["chained"] is False
    assert added["chained"] is False
    assert added["kind"] == "finding_added"
    assert added["text"] == "Hunt run-1 added alert f-1"
    assert added["at"] == "2026-06-01T10:05:00.250000+00:00"
    assert report["text"] == "agent_run_report"


def test_a_malformed_activity_is_skipped_not_fatal():
    activities = [
        "not an entry",
        {"activity_type": "note", "description": "no stamp"},
        {"timestamp": "yesterday", "activity_type": "note", "description": "bad"},
        {"timestamp": "2026-06-01T12:00:00+02:00", "activity_type": "note"},
    ]
    rows = merge_record([], [], activities)
    assert [(row["id"], row["at"]) for row in rows] == [
        ("activity:3", "2026-06-01T10:00:00+00:00")
    ]


def test_activities_stamped_in_one_instant_list_the_later_entry_first():
    stamp = "2026-06-01T10:00:00Z"
    activities = [
        {"timestamp": stamp, "activity_type": "agent_run_report"},
        {"timestamp": stamp, "activity_type": "agent_run_handoff"},
    ]
    rows = merge_record([], [], activities)
    assert [row["kind"] for row in rows] == ["agent_run_handoff", "agent_run_report"]
