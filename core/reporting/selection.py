"""Jurisdictions and data or organisation kinds the Day 1 Report will read (#5010).

One ``system_config`` row, ``day1_report.selection``. A missing row is nothing
selected. A row that does not parse raises ``SelectionError``: callers must
not substitute the empty selection. Writes accept only ids from the catalogue
below, and only a set that is already closed. The form closes a set; this
module does not.
"""

from __future__ import annotations

import logging
from typing import Literal, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.storage.config_service import get_config_service

logger = logging.getLogger(__name__)

KEY = "day1_report.selection"

# Shown on a stored kind whose jurisdiction is not selected. Kept, not dropped.
INACTIVE_NOTICE = "ticked, but its jurisdiction isn't selected: changes nothing"


class Jurisdiction(NamedTuple):
    id: str
    sentence: str
    parent: Optional[str]


class Kind(NamedTuple):
    id: str
    sentence: str
    jurisdiction: Optional[str]
    brings: tuple[str, ...]
    excludes: tuple[str, ...]


# Four jurisdictions. New York stores its parent, the United States.
JURISDICTIONS: tuple[Jurisdiction, ...] = (
    Jurisdiction("united_states", "United States", None),
    Jurisdiction("new_york", "New York", "united_states"),
    Jurisdiction("european_union", "European Union", None),
    Jurisdiction("australia", "Australia", None),
)

# Eleven kinds, worded as the epic's content table words them. PCI DSS names
# no jurisdiction. The cardholder kind brings the assessment kind. The two
# New York kinds exclude each other.
KINDS: tuple[Kind, ...] = (
    Kind(
        "pci_dss_assessment",
        "Vigil is in scope of your PCI DSS assessment",
        None,
        (),
        (),
    ),
    Kind(
        "cardholder_data_environment",
        "Vigil is inside your cardholder data environment",
        None,
        ("pci_dss_assessment",),
        (),
    ),
    Kind(
        "ftc_financial_institution",
        "your organisation is a financial institution under the FTC's jurisdiction",
        "united_states",
        (),
        (),
    ),
    Kind(
        "dfs_covered_entity",
        "your organisation is a DFS covered entity with no Part 500 exemption",
        "new_york",
        (),
        ("dfs_limited_exemption",),
    ),
    Kind(
        "dfs_limited_exemption",
        "your organisation is a DFS covered entity under the 500.19(a) limited exemption",
        "new_york",
        (),
        ("dfs_covered_entity",),
    ),
    Kind(
        "hipaa",
        "your organisation is a HIPAA covered entity or business associate, "
        "and Vigil handles its ePHI",
        "united_states",
        (),
        (),
    ),
    Kind("dod_it", "Vigil is DoD IT", "united_states", (), ()),
    Kind(
        "criminal_justice_information",
        "Vigil processes, stores or transmits criminal-justice information",
        "united_states",
        (),
        (),
    ),
    Kind(
        "fedramp",
        "Vigil is part of a FedRAMP cloud service offering",
        "united_states",
        (),
        (),
    ),
    Kind(
        "federal_tax_information",
        "Vigil handles federal tax information",
        "united_states",
        (),
        (),
    ),
    Kind(
        "non_corporate_commonwealth_entity",
        "your organisation is a non-corporate Commonwealth entity",
        "australia",
        (),
        (),
    ),
)

_JURISDICTION_IDS = {row.id: row for row in JURISDICTIONS}
_KIND_IDS = {row.id: row for row in KINDS}


class SelectionError(Exception):
    """The row is present and does not parse."""


class SelectionRejected(Exception):
    """The write is not a closed set of offered ids."""


class SelectionWriteFailed(Exception):
    """The config row did not save."""


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jurisdictions: list[str] = Field(max_length=50)
    kinds: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def _ids_are_real(self):
        for values in (self.jurisdictions, self.kinds):
            if any(not value.strip() or value != value.strip() for value in values):
                raise ValueError("blank id")
            if len(set(values)) != len(values):
                raise ValueError("duplicate id")
        return self


class CatalogueJurisdiction(BaseModel):
    id: str
    sentence: str
    parent: Optional[str] = None


class CatalogueKind(BaseModel):
    id: str
    sentence: str
    jurisdiction: Optional[str] = None
    brings: list[str]
    excludes: list[str]


class Catalogue(BaseModel):
    jurisdictions: list[CatalogueJurisdiction]
    kinds: list[CatalogueKind]


class UnofferedId(BaseModel):
    id: str
    axis: Literal["jurisdiction", "kind"]


class InactiveKind(BaseModel):
    id: str
    notice: str


class SelectionView(BaseModel):
    jurisdictions: list[str]
    kinds: list[str]
    unoffered: list[UnofferedId]
    inactive: list[InactiveKind]
    catalogue: Catalogue


def catalogue() -> Catalogue:
    return Catalogue(
        jurisdictions=[
            CatalogueJurisdiction(id=row.id, sentence=row.sentence, parent=row.parent)
            for row in JURISDICTIONS
        ],
        kinds=[
            CatalogueKind(
                id=row.id,
                sentence=row.sentence,
                jurisdiction=row.jurisdiction,
                brings=list(row.brings),
                excludes=list(row.excludes),
            )
            for row in KINDS
        ],
    )


def _blank(label: str, values: list[str]) -> None:
    if any(not value.strip() or value != value.strip() for value in values):
        raise SelectionRejected(f"A {label} id is empty")
    if len(set(values)) != len(values):
        raise SelectionRejected(f"A {label} is listed twice")


def rejection(jurisdictions: list[str], kinds: list[str]) -> Optional[str]:
    """Why this set cannot be stored, or None when it is already closed.

    Unknown ids, a kind without its jurisdiction, a sub-jurisdiction without
    its parent, the cardholder kind without the assessment kind, or both New
    York kinds. Does not change the set.
    """
    _blank("jurisdiction", jurisdictions)
    _blank("kind", kinds)
    unknown = [i for i in jurisdictions if i not in _JURISDICTION_IDS] + [
        i for i in kinds if i not in _KIND_IDS
    ]
    if unknown:
        return "Unknown id: " + ", ".join(unknown)
    chosen_j = set(jurisdictions)
    chosen_k = set(kinds)
    for row in JURISDICTIONS:
        if row.id in chosen_j and row.parent and row.parent not in chosen_j:
            parent = _JURISDICTION_IDS[row.parent]
            return f"{row.sentence} requires {parent.sentence}"
    for row in KINDS:
        if row.id not in chosen_k:
            continue
        if row.jurisdiction and row.jurisdiction not in chosen_j:
            place = _JURISDICTION_IDS[row.jurisdiction]
            return f"{row.sentence} requires {place.sentence}"
        for brought_id in row.brings:
            if brought_id not in chosen_k:
                brought = _KIND_IDS[brought_id]
                return f"{row.sentence} requires {brought.sentence}"
    reported: set[frozenset[str]] = set()
    for row in KINDS:
        if row.id not in chosen_k:
            continue
        for other_id in row.excludes:
            pair = frozenset((row.id, other_id))
            if other_id in chosen_k and pair not in reported:
                reported.add(pair)
                other = _KIND_IDS[other_id]
                return f"{row.sentence} and {other.sentence} exclude each other"
    return None


def read_view() -> SelectionView:
    """The stored selection plus the catalogue.

    A missing row is no jurisdiction and no kind. A row that does not parse
    raises ``SelectionError``. A failed read raises from the config service.
    Stored ids this release does not offer stay on the selection and are also
    listed on ``unoffered``.
    """
    value = get_config_service().read_system_config(KEY)
    if value is None:
        stored_j: list[str] = []
        stored_k: list[str] = []
    else:
        try:
            stored = _Stored.model_validate(value)
        except ValidationError as exc:
            logger.error("day1 selection row does not parse")
            raise SelectionError from exc
        stored_j = list(stored.jurisdictions)
        stored_k = list(stored.kinds)
    chosen_j = set(stored_j)
    unoffered = [
        UnofferedId(id=item, axis="jurisdiction")
        for item in stored_j
        if item not in _JURISDICTION_IDS
    ] + [
        UnofferedId(id=item, axis="kind") for item in stored_k if item not in _KIND_IDS
    ]
    inactive = [
        InactiveKind(id=row.id, notice=INACTIVE_NOTICE)
        for row in KINDS
        if row.id in stored_k and row.jurisdiction and row.jurisdiction not in chosen_j
    ]
    return SelectionView(
        jurisdictions=stored_j,
        kinds=stored_k,
        unoffered=unoffered,
        inactive=inactive,
        catalogue=catalogue(),
    )


def write_selection(user_id: str, jurisdictions: list[str], kinds: list[str]) -> None:
    """Store a closed set of offered ids, audited as ``user_id``.

    Raises ``SelectionRejected`` when the set is not already closed.
    """
    reason = rejection(jurisdictions, kinds)
    if reason:
        raise SelectionRejected(reason)
    saved = get_config_service(user_id=user_id).set_system_config(
        key=KEY,
        value={"jurisdictions": jurisdictions, "kinds": kinds},
        description="Jurisdictions and data or organisation kinds",
        config_type="report",
        change_reason="Updated the jurisdictions and data or organisation kinds",
    )
    if not saved:
        raise SelectionWriteFailed
