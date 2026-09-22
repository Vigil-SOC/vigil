"""Merging two Cases keeps the higher rating; unknown ranks below low (#998)."""

from __future__ import annotations

import pytest

from core.cases.case_workflow_service import preferred_merge_priority

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("target", "source", "kept"),
    [
        ("unknown", "high", "high"),
        ("high", "unknown", "high"),
        ("unknown", "unknown", "unknown"),
        ("low", "critical", "critical"),
        ("high", "medium", "high"),
        ("unknown", "not-a-band", "unknown"),
        ("high", "not-a-band", "high"),
    ],
)
def test_preferred_merge_priority(target, source, kept):
    assert preferred_merge_priority(target, source) == kept
