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
