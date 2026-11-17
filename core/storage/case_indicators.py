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

from core.storage.entity_context import context_candidates
from core.storage.models import CaseIOC, Finding, case_findings

# Role-less: where an IP or hash sits in a context is all they say about it.
EXTRA_LISTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ip", ("source_ips", "ips", "ip_addresses")),
    ("hash", ("hashes",)),
)

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


def context_indicators(context: Any) -> List[Tuple[str, str]]:
    """The ``(ioc_type, value)`` pairs one ``entity_context`` names.

    An IP's type is its role (``src_ip``, ``dst_ip``) where the field says one.
    """
    found = [(typed, value) for _, value, typed in context_candidates(_wrap(context))]
    if isinstance(context, Mapping):
        for kind, names in EXTRA_LISTS:
            for name in names:
                raw = context.get(name)
                for item in raw if isinstance(raw, (list, tuple)) else [raw]:
                    if isinstance(item, (str, int)) and str(item).strip():
                        found.append((kind, str(item).strip()))
    return found


def _wrap(context: Any) -> dict:
    return {"entity_context": context}


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
