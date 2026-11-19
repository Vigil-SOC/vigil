"""A Case's indicators: what the shared IOC index holds for it, and what intake can use.

One extractor over the two places a Case names an indicator: its ``CaseIOC`` rows
and the ``entity_context`` of its findings. It returns ``(ioc_type, value)`` as
named; ``SharedIOCRepository.make_key`` spells them. Storage may not import
memory, so the field map is ``entity_context``'s, and the role-less spellings
sources also write live here, not there: memory's Entity Keys must not change.
"""

from typing import Any, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.storage.entity_context import CONTEXT_LISTS, context_candidates
from core.storage.models import CaseIOC, Finding, case_findings

# Role-less: where an IP or hash sits in a context is all they say about it.
EXTRA_LISTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ip", ("source_ips", "ips", "ip_addresses")),
    ("hash", ("hashes",)),
)

# List-field names only. A lone string under one of these is still one value;
# a scalar (``src_ip``, ``dst_ip``, ``hostname``, ``user``) stays as stored so
# ``context_candidates`` stringifies the value itself, not ``str`` of a list.
_LIST_FIELDS = frozenset(
    name for _, names, _ in CONTEXT_LISTS for name in names
) | frozenset(name for _, names in EXTRA_LISTS for name in names)


# What intake can do with a key, by its type. A source IP or a hash attaches an
# alert; a destination IP, or an IP whose role was never recorded, only links;
# a hostname attaches only on the same data_source and title. Nothing else is
# written to the index: other types stay on the Case alone.
ATTACHES = ("src_ip", "hash")
LINK_ONLY_TYPES = ("dst_ip", "ip")
SAME_TITLE = ("hostname",)
INDEXED_TYPES = frozenset(ATTACHES + LINK_ONLY_TYPES + SAME_TITLE)


def intake_use(key_type: str) -> Optional[str]:
    """``attach``, ``link`` or ``same_title`` for a keyed type intake reads; else None."""
    if key_type in ATTACHES:
        return "attach"
    if key_type in LINK_ONLY_TYPES:
        return "link"
    return "same_title" if key_type in SAME_TITLE else None


def _items(raw: Any) -> List[str]:
    """A field's values: a lone string or number counts as a list of one."""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [
        str(i).strip() for i in items if isinstance(i, (str, int)) and str(i).strip()
    ]


def context_indicators(context: Any) -> List[Tuple[str, str]]:
    """The ``(ioc_type, value)`` pairs one ``entity_context`` names.

    An IP's type is its role (``src_ip``, ``dst_ip``) where the field says one.
    A list field is cleaned to strings first: a source writes one as a lone
    value or with junk in it. Scalar fields are left as stored.
    """
    if not isinstance(context, Mapping):
        return []
    cleaned = {
        name: (
            _items(raw)
            if name in _LIST_FIELDS and isinstance(raw, (list, tuple, str, int))
            else raw
        )
        for name, raw in context.items()
    }
    found = [
        (typed, value)
        for _, value, typed in context_candidates({"entity_context": cleaned})
    ]
    for kind, names in EXTRA_LISTS:
        found += [(kind, v) for name in names for v in _items(context.get(name))]
    return found


def case_indicators(session: Session, case_id: str) -> Set[Tuple[str, str]]:
    """Every indicator a Case holds: its recorded IOCs and its findings' contexts."""
    found: Set[Tuple[str, str]] = {
        (t, v)
        for t, v in session.execute(
            select(CaseIOC.ioc_type, CaseIOC.value).where(CaseIOC.case_id == case_id)
        )
    }
    contexts: Iterable[Any] = session.scalars(
        select(Finding.entity_context)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .where(case_findings.c.case_id == case_id)
    )
    for context in contexts:
        found.update(context_indicators(context))
    return found
