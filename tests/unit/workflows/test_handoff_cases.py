# The run page names the case a hunt's handoff was filed on: the case the hunt was
# started from, else the one the bridge opened for it -- never the hunt's own key.

from __future__ import annotations

import pytest

from core.workflows import run_bridge_router as rbr
from core.workflows.handoff_cases import (
    filed_case,
    handoff_case_id,
    stamp_handoff_cases,
)

pytestmark = pytest.mark.unit


def _folded():
    return {
        "handoffs": [
            {"case_id": "case-25aac39c", "hypothesis_id": "h-1", "iteration": 4},
        ]
    }


def test_a_hunt_with_no_case_points_at_the_case_the_bridge_opens():
    folded = _folded()
    stamp_handoff_cases(folded, "run-1", None)
    [handoff] = folded["handoffs"]
    assert handoff["case_id"] == handoff_case_id("run-1", "case-25aac39c")
    assert handoff["case_id"].startswith("case-handoff-")
    assert handoff["handoff_key"] == "case-25aac39c"


def test_a_hunt_started_on_a_case_points_at_that_case():
    folded = _folded()
    stamp_handoff_cases(folded, "run-1", "case-origin")
    assert folded["handoffs"][0]["case_id"] == "case-origin"
    assert filed_case("run-1", "case-25aac39c", "case-origin") == "case-origin"


def test_the_bridge_and_the_run_page_name_the_same_case():
    handoff = rbr.TerminalHandoff(case_id="case-25aac39c", title="t")
    folded = _folded()
    stamp_handoff_cases(folded, "run-1", None)
    assert rbr._handoff_case_id("run-1", handoff) == folded["handoffs"][0]["case_id"]


def test_a_projection_without_handoffs_is_left_alone():
    for folded in (None, {}, {"handoffs": None}, {"handoffs": [{"case_id": ""}]}):
        stamp_handoff_cases(folded, "run-1", None)
