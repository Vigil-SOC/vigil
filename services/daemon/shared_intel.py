"""The entity index intake joins alerts to Cases by, over ``shared_iocs``.

Nothing is held in process, so it survives a daemon restart. Matching a finding
to a Case is ``SharedIOCRepository.matching_cases``.
"""

import logging
from typing import Any, Callable, Dict, Iterable, Optional, Set

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

    def record_on_case(self, case_id: str, finding: Dict[str, Any]):
        """Index a merged finding's entities against the Case's newest investigation."""
        keys = _keys_from_finding(finding)
        if not keys:
            return
        self._with_repo(
            "merge write", lambda repo: repo.record_on_case(case_id, keys), None
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
