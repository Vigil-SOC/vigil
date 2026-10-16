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


def _kernel_hunt() -> list[dict]:
    """A kernel hunt's ledger (#2266), as the agent returns it, oldest first."""
    rows = [
        ("run", {"run_kind": "hunt"}),
        (
            "hypothesis",
            {
                "hypothesis_id": "h-1",
                "statement": "FIN-WS-114 beacons",
                "provenance": "operator",
            },
        ),
        (
            "hypothesis",
            {
                "hypothesis_id": "h-0",
                "statement": "it is backup traffic",
                "provenance": "base_rate",
            },
        ),
        (
            "checkpoint",
            {
                "checkpoint_id": "cp-1",
                "question": "Approve and start this hunt on 2 explanations",
            },
        ),
        (
            "resolution",
            {"checkpoint_id": "cp-1", "answer": "approve", "actor": "ada", "text": ""},
        ),
        ("frame", {"role": "agent", "call": 1}),
        ("content", {"hash": "x", "text": "..."}),
        ("spend", {"role": "agent", "cost_usd": 0.0015}),
        (
            "call",
            {
                "tool": "search_findings",
                "arguments": '{"query": "HTTPS beaconing", "limit": 20}',
                "ok": True,
                "turn": 1,
            },
        ),
        (
            "call",
            {
                "tool": "record_evidence",
                "arguments": '{"results": [{"summary": "212 HTTPS connections"}]}',
                "ok": True,
                "turn": 2,
            },
        ),
        (
            "evidence",
            {
                "evidence_id": "ev-1",
                "summary": "212 HTTPS connections at 60s to cdn-invoicesync.net",
                "provenance": "worker",
            },
        ),
        (
            "evidence",
            {
                "evidence_id": "ev-2",
                "summary": "no tool in this deployment answers telemetry_search",
                "provenance": "deployment_gap",
            },
        ),
        (
            "link",
            {"evidence_id": "ev-1", "hypothesis_id": "h-1", "relation": "supports"},
        ),
        (
            "link",
            {"evidence_id": "ev-1", "hypothesis_id": "h-0", "relation": "neither"},
        ),
        ("patch", {"target": "hunt", "id": "hunt-1", "fields": {"iteration": 2}}),
        (
            "patch",
            {
                "target": "hypothesis",
                "id": "h-1",
                "fields": {
                    "status": "proven",
                    "resolution_reason": "survived the null",
                },
            },
        ),
        (
            "question",
            {
                "question_id": "q-1",
                "question": "Which host ran the MiniDump?",
                "status": "open",
            },
        ),
        (
            "call",
            {
                "tool": "conclude_hunt",
                "arguments": '{"reason": "done"}',
                "ok": False,
                "failure": "open question q-1 is still open",
                "turn": 3,
            },
        ),
    ]
    return [
        {
            "seq": seq,
            "ts": f"2026-10-14T16:00:{seq:02d}Z",
            "kind": kind,
            "payload": payload,
        }
        for seq, (kind, payload) in enumerate(rows)
    ]


def test_kernel_hunt_rows_read_as_what_happened():
    texts = [row["text"] for row in reversed(merge_record(_kernel_hunt(), []))]
    assert texts == [
        "Run started",
        "Explanation put up — FIN-WS-114 beacons",
        "Benign explanation to beat — it is backup traffic",
        "Asked you — Approve and start this hunt on 2 explanations",
        "Approved by ada: Approve and start this hunt on 2 explanations",
        "Searched alerts: HTTPS beaconing",
        "212 HTTPS connections at 60s to cdn-invoicesync.net",
        "Blind spot — no tool in this deployment answers telemetry_search",
        "Supports “FIN-WS-114 beacons” — 212 HTTPS connections at 60s to cdn-invoicesync.net",
        "Explanation proven: “FIN-WS-114 beacons” — survived the null",
        "Lead opened — Which host ran the MiniDump?",
        "Refused — Asked to conclude the hunt: done — open question q-1 is still open",
    ]


def test_kernel_bookkeeping_is_left_out_and_no_row_reads_as_its_kind():
    rows = merge_record(_kernel_hunt(), [])
    kinds = {row["kind"] for row in rows}
    # Frames, prices, counters and a "neither" ruling change nothing a person reads.
    assert not kinds & {"frame", "content", "spend"}
    assert sum(row["kind"] == "link" for row in rows) == 1
    assert sum(row["kind"] == "patch" for row in rows) == 1
    # A record_evidence call is its evidence row; the call adds nothing.
    assert not any("record_evidence" in row["text"] for row in rows)
    for row in rows:
        assert row["text"] != row["kind"]
        assert not row["text"].endswith("chained")
        assert "{" not in row["text"]


def test_a_report_activity_reads_as_plain_words_not_markdown():
    activities = [
        {
            "timestamp": "2026-10-14T16:51:00Z",
            "activity_type": "agent_run_report",
            "description": "# Hunt report — threat-hunt\n- **Outcome:** completed\n- **Iterations:** 40 of 48",
        }
    ]
    [row] = merge_record([], [], activities)
    assert (
        row["text"]
        == "Hunt report — threat-hunt Outcome: completed Iterations: 40 of 48"
    )
