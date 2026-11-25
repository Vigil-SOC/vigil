"""Erase the re-decision ledgers an import left behind (#5096).

``delete_import`` does not call this. The import is already gone, and the
caller passes only that import's marked runs. A run a Case was opened from
is not passed; nothing records that link yet, so ``cases`` is empty.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from sqlalchemy import String, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.storage.models import HistoryImport, WorkflowRun
from core.workflows.redecision_hold import MARK

LEDGERS_ERASED = "memory.ledgers.erased"

_ERASE = text("SELECT public.erase_redecision_ledgers(:run_ids)").bindparams(
    bindparam("run_ids", type_=ARRAY(String()))
)


class ImportStillThere(RuntimeError):
    """The import row is still present."""


class RunNotMarked(RuntimeError):
    """An id is not a marked run of this import. Nothing was erased."""


def marked_run_ids(session: Session, import_id: str) -> List[str]:
    """Run ids whose trigger context is a re-decision of this import."""
    rows = session.scalars(
        select(WorkflowRun.run_id)
        .where(
            WorkflowRun.trigger_context[MARK].astext == "true",
            WorkflowRun.trigger_context["import_id"].astext == import_id,
        )
        .order_by(WorkflowRun.run_id)
    )
    return list(rows)


def erase_ledgers(
    session: Session,
    import_id: str,
    run_ids: Sequence[str],
    *,
    actor: str,
) -> Dict[str, Any]:
    """Delete the ledgers and the run rows. Raises before any delete otherwise.

    ``cases`` names Cases a run was opened from. Those ids are not passed to
    the function. None are recorded yet.
    """
    if session.get(HistoryImport, import_id) is not None:
        raise ImportStillThere(import_id)
    owned = set(marked_run_ids(session, import_id))
    if not run_ids or any(run_id not in owned for run_id in run_ids):
        raise RunNotMarked(import_id)
    try:
        removed = session.execute(_ERASE, {"run_ids": list(run_ids)}).scalar_one()
    except DBAPIError as exc:
        # 22023 is the function refusing an unmarked id. 22P02 is a run id
        # that is not a uuid, so it cannot be a ledger row. Either deletes nothing.
        code = getattr(getattr(exc, "orig", None), "pgcode", None)
        if code not in ("22023", "22P02"):
            raise
        raise RunNotMarked(import_id) from exc
    trail_events.record(
        session,
        LEDGERS_ERASED,
        actor=actor,
        import_id=import_id,
        run_ids=list(run_ids),
    )
    return {"erased": int(removed), "cases": []}
