"""Record is one merged read, newest first, with audit rows unchained."""

import json
from datetime import datetime, timezone

from core.cases.case_record import merge_record, unread_row
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
    rows = merge_record([("r1", events)], audits)
    assert [row["id"] for row in rows] == ["run:r1:2", "audit:7", "run:r1:1"]
    assert rows[0]["run_id"] == "r1"
    assert "run_id" not in rows[1]
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
    rows = merge_record([("r1", events)], [])
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
    rows = merge_record([("r1", events)], audits, activities)
    assert [row["id"] for row in rows] == [
        "activity:4",
        "run:r1:2",
        "audit:7",
        "activity:0",
        "run:r1:1",
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
    texts = [
        row["text"] for row in reversed(merge_record([("r1", _kernel_hunt())], []))
    ]
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
    rows = merge_record([("r1", _kernel_hunt())], [])
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


def _rows(*rows):
    return [
        {
            "seq": seq,
            "ts": f"2026-10-14T17:00:{seq:02d}Z",
            "kind": kind,
            "payload": payload,
        }
        for seq, (kind, payload) in enumerate(rows)
    ]


def test_a_search_reads_as_its_query_and_how_many_rows_came_back():
    rows = _rows(
        (
            "call",
            {
                "tool": "splunk-mcp_splunk_execute",
                "arguments": '{"spl_query": "index=botsv3 sourcetype=stream:tcp | stats count by dest_ip"}',
                "ok": True,
                "result": '<vigil:tool_result tool="splunk_execute" ref="r6">\n1 row(s) from splunk\n[]\n</vigil:tool_result>',
                "turn": 1,
            },
        ),
        (
            "call",
            {
                "tool": "splunk-mcp_splunk_execute",
                "arguments": '{"spl_query": "index=botsv3 | head 1"}',
                "ok": False,
                "failure": {"kind": "timeout", "detail": "no answer in 30s"},
                "turn": 1,
            },
        ),
        (
            "call",
            {
                "tool": "splunk-mcp_splunk_execute",
                "arguments": '{"spl_query": "index=botsv3 | stats count by host"}',
                "ok": True,
                "result": '<vigil:tool_result tool="splunk_execute" ref="r7">\n1 row(s) from splunk\n'
                + json.dumps(
                    [
                        {
                            "success": True,
                            "results": [{"host": "a"}, {"host": "b"}, {"host": "c"}],
                        }
                    ]
                )
                + "\n</vigil:tool_result>",
                "turn": 2,
            },
        ),
        ("dispatch", {"agent_id": "agent"}),
    )
    texts = [row["text"] for row in reversed(merge_record([("run", rows)], []))]
    assert texts == [
        "Searched Splunk: index=botsv3 sourcetype=stream:tcp | stats count by dest_ip → 1 row",
        "Refused — Searched Splunk: index=botsv3 | head 1 — timeout — no answer in 30s",
        # The wrapper Splunk answers in is one row; its results are what came back.
        "Searched Splunk: index=botsv3 | stats count by host → 3 rows",
    ]


def test_root_cause_and_phase_rows_read_as_words_not_their_kind():
    rows = _rows(
        (
            "step",
            {
                "step_id": "s2",
                "event": "powershell.exe started iexeplorer.exe",
                "who": "FYODOR-L\\fyodor",
                "cause_id": "s1",
                "link": "parent_process_id",
                "link_status": "proven",
            },
        ),
        ("notice", {"text": "No process telemetry before 10:00"}),
        (
            "phase",
            {
                "name": "triage",
                "agent": "triage-agent",
                "answer": {"summary": "Two hosts beacon"},
            },
        ),
        (
            "conclusion",
            {"value": {"text": "The chain starts at a phishing attachment"}},
        ),
    )
    texts = [row["text"] for row in reversed(merge_record([("run", rows)], []))]
    assert texts == [
        "Step s2 — powershell.exe started iexeplorer.exe — FYODOR-L\\fyodor — caused by s1 via parent_process_id (proven)",
        "Notice — No process telemetry before 10:00",
        "Phase triage by triage-agent — Two hosts beacon",
        "Concluded — The chain starts at a phishing attachment",
    ]


def test_a_tie_with_no_link_name_drops_the_via_and_a_host_search_names_its_host():
    rows = _rows(
        (
            "step",
            {
                "step_id": "s2",
                "event": "x",
                "cause_id": "s1",
                "link": "",
                "link_status": "proven",
            },
        ),
        (
            "call",
            {
                "tool": "splunk_search_host",
                "arguments": '{"hostname": "FYODOR-L"}',
                "ok": True,
                "result": '<vigil:tool_result tool="splunk_search_host" ref="r8">\n2 row(s) from splunk\n[]\n</vigil:tool_result>',
                "turn": 3,
            },
        ),
    )
    texts = [row["text"] for row in reversed(merge_record([("run", rows)], []))]
    assert texts == [
        "Step s2 — x — caused by s1 (proven)",
        "Searched Splunk: FYODOR-L → 2 rows",
    ]


def _link_run(hypothesis_id: str, statement: str, at: str) -> list[dict]:
    return [
        {
            "seq": 1,
            "ts": f"{at}:00Z",
            "kind": "hypothesis",
            "payload": {"hypothesis_id": hypothesis_id, "statement": statement},
        },
        {
            "seq": 2,
            "ts": f"{at}:02Z",
            "kind": "link",
            "payload": {
                "hypothesis_id": hypothesis_id,
                "evidence_id": "e-1",
                "relation": "supports",
            },
        },
    ]


def test_two_runs_sort_together_with_distinct_ids_and_their_own_ids_named():
    # Both runs call their explanation h-1 and both ledgers start at seq 1.
    hunt = _link_run("h-1", "beacons", "2026-10-14T16:00")
    root = _link_run("h-1", "phished login", "2026-10-14T16:01")
    rows = merge_record([("hunt", hunt), ("root", root)], [])
    assert [row["id"] for row in rows] == [
        "run:root:2",
        "run:root:1",
        "run:hunt:2",
        "run:hunt:1",
    ]
    assert rows[0]["text"] == "Supports “phished login” — e-1"
    assert rows[2]["text"] == "Supports “beacons” — e-1"


def test_an_unread_run_is_one_unchained_row_in_time_order():
    row = unread_row("gone", "2026-10-14T16:00:01", "the agent layer answered 500")
    rows = merge_record(
        [("hunt", _link_run("h-1", "beacons", "2026-10-14T16:00"))], [], (), [row]
    )
    assert [one["id"] for one in rows] == [
        "run:hunt:2",
        "run:gone:unread",
        "run:hunt:1",
    ]
    assert rows[1] == {
        "id": "run:gone:unread",
        "at": "2026-10-14T16:00:01+00:00",
        "kind": "unread",
        "source": "run",
        "chained": False,
        "text": "Could not read the record of run gone — the agent layer answered 500",
        "run_id": "gone",
    }
