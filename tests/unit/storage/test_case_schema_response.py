"""CaseSchema must round-trip through FastAPI's response_model re-validation.

``dump()`` emits ``finding_ids: list[str]``. Annotating GET/POST with
``response_model=CaseSchema`` makes FastAPI validate that dict, so the
``findings``-relationship validator has to accept already-serialized ids.
"""

import pytest

from core.storage.schemas.case import CaseSchema
from core.storage.schemas.case_entities import CaseSLAStatusSchema

pytestmark = pytest.mark.unit


class _Finding:
    def __init__(self, finding_id: str):
        self.finding_id = finding_id


class _Case:
    case_id = "case-1"
    title = "Example"
    description = None
    status = "open"
    priority = "medium"
    assignee = None
    tags = None
    notes = None
    timeline = None
    activities = None
    resolution_steps = None
    mitre_techniques = None
    created_at = None
    updated_at = None
    findings = [_Finding("f-1"), _Finding("f-2")]


def test_dump_extracts_ids_from_the_findings_relationship():
    assert CaseSchema.dump(_Case())["finding_ids"] == ["f-1", "f-2"]


def test_dumped_case_revalidates_as_response_model():
    dumped = CaseSchema.dump(_Case())
    again = CaseSchema.model_validate(dumped).model_dump(mode="json")
    assert again["finding_ids"] == ["f-1", "f-2"]
    assert "findings" not in again


def test_finding_ids_from_inlined_dicts():
    parsed = CaseSchema.model_validate(
        {"case_id": "c1", "findings": [{"finding_id": "f-9"}]}
    )
    assert parsed.finding_ids == ["f-9"]


def test_sla_status_schema_fields_match_get_sla_status():
    # Keys of the dict CaseSLAService.get_sla_status returns.
    assert set(CaseSLAStatusSchema.model_fields) == {
        "case_id",
        "sla_policy_id",
        "response_due",
        "resolution_due",
        "response_remaining_seconds",
        "resolution_remaining_seconds",
        "response_percent_elapsed",
        "resolution_percent_elapsed",
        "response_completed",
        "resolution_completed",
        "response_sla_met",
        "resolution_sla_met",
        "is_breached",
        "breach_type",
        "is_paused",
        "health_status",
    }
