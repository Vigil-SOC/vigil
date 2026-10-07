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
    """Accept ORM findings, dumped ids, or inlined finding dicts.

    ``dump()`` reads the ``findings`` relationship; FastAPI then re-validates
    the dumped dict (``finding_ids: list[str]``) when this schema is a
    ``response_model``. Both shapes have to round-trip.
    """
    if not value:
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            fid = item.get("finding_id")
            if isinstance(fid, str):
                ids.append(fid)
        else:
            ids.append(item.finding_id)
    return ids


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
