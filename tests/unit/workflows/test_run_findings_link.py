# A hunt's evidence cites alerts; the backend links them to the hunt's own case so
# its alert count, entities and IOCs read from them.

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from core.workflows.run_bridge_router import CitedFindings, record_findings

pytestmark = pytest.mark.unit


class _Data:
    def __init__(self, case: Dict[str, Any], findings: set):
        self.cases = {"case-1": case}
        self.findings = findings

    def get_case(self, case_id: str):
        return self.cases.get(case_id)

    def update_case(self, case_id: str, **updates):
        self.cases[case_id].update(updates)
        return True

    def get_finding(self, finding_id: str):
        return {"finding_id": finding_id} if finding_id in self.findings else None


class _Runs:
    def __init__(self, trigger_context: Dict[str, Any]):
        self.trigger_context = trigger_context

    def get_run(self, run_id: str):
        return {"run_id": run_id, "trigger_context": self.trigger_context}


def _push(data: _Data, ids, context=None):
    with patch(
        "core.storage.database_data_service.DatabaseDataService", return_value=data
    ), patch("core.workflows.run_bridge_router.authorise"):
        record_findings(
            "run-1",
            CitedFindings(finding_ids=ids),
            authorization="Bearer x",
            run_service=_Runs({"case_id": "case-1"} if context is None else context),
        )


def test_links_each_cited_alert_once_and_audits_the_hunt_adding_it():
    data = _Data({"finding_ids": ["demo2-ts-001"]}, {"demo2-ts-001", "demo2-ts-004"})

    _push(data, ["demo2-ts-004", "demo2-ts-004", "demo2-ts-001"])
    _push(data, ["demo2-ts-004"])  # a resumed hunt pushes it again

    case = data.cases["case-1"]
    assert case["finding_ids"] == ["demo2-ts-001", "demo2-ts-004"]
    added = [a for a in case["activities"] if a["activity_type"] == "finding_added"]
    assert [a["details"] for a in added] == [
        {"run_id": "run-1", "finding_id": "demo2-ts-004"}
    ]
    assert "demo2-ts-004" in added[0]["description"]


def test_an_id_that_names_no_finding_is_not_linked():
    data = _Data({"title": "hunt"}, {"demo2-ts-001"})

    _push(data, ["made-up", "demo2-ts-001"])

    assert data.cases["case-1"]["finding_ids"] == ["demo2-ts-001"]


def test_a_hunt_with_no_case_links_nothing():
    data = _Data({"title": "hunt"}, {"demo2-ts-001"})

    _push(data, ["demo2-ts-001"], context={})

    assert "finding_ids" not in data.cases["case-1"]
