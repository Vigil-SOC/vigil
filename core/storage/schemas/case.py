"""Serialization schemas for the Case model.

A case renders its findings one of two ways — as a list of ids, or fully
inlined — so the two shapes are separate schemas rather than one schema with
a flag. Both derive the value from the ``findings`` relationship, which is
the only link that exists; there is no ``finding_ids`` column.
"""

from typing import Annotated, Any, Optional

from pydantic import BeforeValidator, Field

from core.storage.schemas.base import JsonList, OptDateTime, ORMSchema, StrList
from core.storage.schemas.finding import FindingSchema


def _to_finding_ids(value: Any) -> Any:
    if not value:
        return []
    return [finding.finding_id for finding in value]


FindingIds = Annotated[list[str], BeforeValidator(_to_finding_ids)]


class CaseBaseSchema(ORMSchema):
    """Case fields common to both rendering modes."""

    case_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    tags: StrList = []
    notes: JsonList = []
    timeline: JsonList = []
    activities: JsonList = []
    resolution_steps: JsonList = []
    mitre_techniques: StrList = []
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseSchema(CaseBaseSchema):
    """Default shape — findings referenced by id."""

    finding_ids: FindingIds = Field(default_factory=list, validation_alias="findings")


class CaseWithFindingsSchema(CaseBaseSchema):
    """Detail shape — findings inlined in full."""

    findings: list[FindingSchema] = Field(default_factory=list)
