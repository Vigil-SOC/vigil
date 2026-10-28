"""Cross-investigation IOC index over ``shared_iocs``.

Nothing is held in process, so a match survives a daemon restart. Intake reads
it through ``matching_cases`` to join an alert to its open Case and to link a
new Case to the closed ones before it.
"""

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from core.memory.entity_keys import entity_context_candidates
from core.storage.connection import get_db_manager
from core.storage.shared_ioc_repository import (
    CaseMatch,
    SharedIOCRepository,
    make_key,
)

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

    def matching_cases(
        self,
        finding: Dict[str, Any],
        *,
        open: bool,
        limit: Optional[int] = None,
        exclude_case_id: Optional[str] = None,
    ) -> List[CaseMatch]:
        """Cases this finding belongs with, newest first. Raises on a failed read.

        Not routed through ``_with_repo``: an empty answer on a failed read would
        launch a new Case for an alert that has one.
        """
        with get_db_manager().session_scope() as session:
            return SharedIOCRepository(session).matching_cases(
                finding,
                entity_context_candidates(finding),
                open=open,
                limit=limit,
                exclude_case_id=exclude_case_id,
            )

    def record_on_case(self, case_id: str, finding: Dict[str, Any]) -> None:
        """Index a merged finding's entities against its Case's newest investigation."""
        keys = _keys_from_finding(finding)
        if not keys:
            return

        def _record(repo: SharedIOCRepository) -> int:
            inv_id = repo.newest_investigation_of(case_id)
            return repo.record(inv_id, keys) if inv_id else 0

        self._with_repo("merge write", _record, None)

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
