"""Approval rows serialize their naive-UTC columns with the offset written."""

from datetime import datetime, timezone

import pytest

from core.response.approval_service import _row_to_pending
from core.storage.models import ApprovalAction

pytestmark = pytest.mark.unit


def _row(**kw) -> ApprovalAction:
    return ApprovalAction(
        action_id="ts-row",
        action_type="block_ip",
        title="t",
        description="d",
        target="host",
        confidence=0.4,
        reason="because",
        evidence=[],
        created_by="pytest",
        requires_approval=True,
        status=kw.pop("status", "pending"),
        created_at=kw.pop("created_at", datetime(2026, 10, 13, 8, 18, 10, 769100)),
        parameters={},
        **kw,
    )


def test_timestamps_carry_utc_offset_with_the_same_wall_clock():
    action = _row_to_pending(
        _row(
            status="executed",
            approved_at=datetime(2026, 10, 13, 8, 20, 0),
            executed_at=datetime(2026, 10, 13, 8, 21, 0),
        )
    )

    assert action.created_at == "2026-10-13T08:18:10.769100+00:00"
    assert action.approved_at == "2026-10-13T08:20:00+00:00"
    assert action.executed_at == "2026-10-13T08:21:00+00:00"
    # Parsed back, each is the same instant the column held as UTC.
    assert datetime.fromisoformat(action.created_at) == datetime(
        2026, 10, 13, 8, 18, 10, 769100, tzinfo=timezone.utc
    )


def test_unset_timestamps_keep_their_empty_values():
    action = _row_to_pending(_row(created_at=None))

    assert action.created_at == ""
    assert action.approved_at is None
    assert action.executed_at is None
