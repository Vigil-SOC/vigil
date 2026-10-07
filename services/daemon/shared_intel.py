"""Cross-investigation IOC correlation over ``shared_iocs``.

Nothing is held in process, so overlap survives a daemon restart. Dedup
(``check_overlap``) is bounded to live investigations; correlation reads the
whole table.
"""

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from core.memory.entity_keys import entity_context_candidates
from core.storage.connection import get_db_manager
from core.storage.shared_ioc_repository import SharedIOCRepository, make_key

logger = logging.getLogger(__name__)


def _keys_from_finding(finding: Dict[str, Any]) -> Set[str]:
    """This finding's entities as shared-IOC keys.

    The spelling map is memory's (``entity_context_candidates``); the keys are
    this table's. ``make_key`` aliases ``host`` back to ``hostname`` on the way
    in, so what lands in ``shared_iocs`` is what always landed there -- and a key
    minted in the wrong vocabulary joins against nothing.
    """
    return {
        key
        for key in (
            make_key(kind, value) for kind, value in entity_context_candidates(finding)
        )
        if key
    }


class SharedIntelligence:
    """Cross-investigation IOC tracker over ``shared_iocs``."""

    def register_investigation(
        self, investigation_id: str, findings: Iterable[Dict[str, Any]]
    ):
        """Index the entities a new investigation's findings name.

        Call after the investigation is saved — ``shared_iocs`` is keyed to it.
        """
        keys: Set[str] = set()
        for finding in findings:
            keys |= _keys_from_finding(finding)
        if not keys:
            return
        self._with_repo(
            "register", lambda repo: repo.record(investigation_id, keys), None
        )

    def check_overlap(
        self, finding: Dict[str, Any], exclude_id: Optional[str] = None
    ) -> List[str]:
        """Live investigations already covering one of this finding's entities."""
        keys = _keys_from_finding(finding)
        if not keys:
            return []

        overlapping = self._with_repo(
            "overlap lookup", lambda repo: repo.open_investigations_for(keys), set()
        )
        if exclude_id:
            overlapping.discard(exclude_id)
        return sorted(overlapping)

    def get_related_investigations(self, investigation_id: str) -> List[str]:
        """Investigations that share an indicator with the given one, ever."""

        def _related(repo: SharedIOCRepository) -> Set[str]:
            keys = repo.keys_for(investigation_id)
            return repo.investigations_for(keys) if keys else set()

        related = self._with_repo("related lookup", _related, set())
        related.discard(investigation_id)
        return sorted(related)

    def get_shared_iocs(self, inv_id_a: str, inv_id_b: str) -> List[str]:
        return sorted(
            self._with_repo(
                "shared lookup",
                lambda repo: repo.shared_between(inv_id_a, inv_id_b),
                set(),
            )
        )

    def close_investigation(self, investigation_id: str, case_id: Optional[str]):
        """Pick up what the case accumulated while the investigation ran."""
        if not case_id:
            return
        self._with_repo(
            "close write",
            lambda repo: repo.record_case_for(case_id, investigation_id),
            None,
        )

    def _with_repo(self, operation: str, fn: Callable, default: Any) -> Any:
        # Reads degrade to default and writes drop rather than taking the
        # daemon loop down; either way the failure is logged loud.
        try:
            with get_db_manager().session_scope() as session:
                return fn(SharedIOCRepository(session))
        except Exception as e:
            logger.error("shared_iocs %s failed: %s", operation, e, exc_info=True)
            return default
