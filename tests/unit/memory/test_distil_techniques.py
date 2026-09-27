"""The Distil carries cited techniques onto Verdict rows (#898), without a store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.memory.distil import Concluded, _conclusion_rows

pytestmark = pytest.mark.unit

CONCLUDED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _conclusion(hypothesis_id, **fields):
    base = {
        "hypothesis_id": hypothesis_id,
        "statement": "beaconing over web protocols",
        "status": "proven",
        "rationale": "two sources agreed",
        "subject_entities": [],
        "evidence_count": 2,
        "attacker_influenceable_only": False,
        "sources": [{"source_system": "dns", "stance": "supports"}],
        "first_seen": "2026-08-30T00:00:00Z",
        "last_seen": "2026-08-31T00:00:00Z",
        "window_observed": True,
    }
    return base | fields


def _rows(*conclusions):
    payload = {"outcome": "completed", "conclusions": list(conclusions)}
    return _conclusion_rows(Concluded(payload, "hunt-898", CONCLUDED_AT))


def test_cited_techniques_land_on_the_verdict_row_only():
    verdicts, gaps = _rows(
        _conclusion("h-1", techniques=["T1071.001", "", "T1071.001", None, "T1568"]),
        _conclusion("h-2", techniques=[]),
        _conclusion("h-3", status="parked", evidence_count=0, techniques=["T1071.001"]),
    )

    by_id = {row["row"]["hypothesis_id"]: row["row"] for row in verdicts}
    assert by_id["h-1"]["techniques"] == ["T1071.001", "T1568"]
    assert by_id["h-2"]["techniques"] == []
    # A Gap has no evidence to have cited anything, and the column is not its.
    assert [gap["hypothesis_id"] for gap in gaps] == ["h-3"]
    assert "techniques" not in gaps[0]


def test_a_payload_without_the_field_yields_an_empty_list():
    verdicts, _ = _rows(_conclusion("h-1"))
    assert verdicts[0]["row"]["techniques"] == []
