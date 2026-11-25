"""Erase the re-decision ledgers an import left behind (#5096, #5095).

``delete_import`` does not call this. The import is already gone, and the
caller passes only that import's marked runs. A run a Case was opened from
is not passed to ``erase_redecision_ledgers``. ``cases`` names those Cases.
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


def _opened_cases(session: Session, run_ids: Sequence[str]) -> List[Dict[str, str]]:
    """Cases whose mark names one of these runs. The run is not erased."""
    rows = session.execute(
        text("""
            SELECT case_id, recheck_mark->>'run_id' AS run_id
            FROM cases
            WHERE recheck_mark->>'run_id' = ANY(:run_ids)
            ORDER BY case_id
            """).bindparams(bindparam("run_ids", type_=ARRAY(String()))),
        {"run_ids": list(run_ids)},
    )
    return [{"case_id": row.case_id, "run_id": row.run_id} for row in rows]


def erase_ledgers(
    session: Session,
    import_id: str,
    run_ids: Sequence[str],
    *,
    actor: str,
) -> Dict[str, Any]:
    """Delete the ledgers and the run rows. Raises before any delete otherwise.

    ``cases`` names Cases a run was opened from. Those run ids are not passed
    to ``erase_redecision_ledgers``.
    """
    if session.get(HistoryImport, import_id) is not None:
        raise ImportStillThere(import_id)
    owned = set(marked_run_ids(session, import_id))
    if not run_ids or any(run_id not in owned for run_id in run_ids):
        raise RunNotMarked(import_id)
    spared = _opened_cases(session, run_ids)
    spared_runs = {row["run_id"] for row in spared}
    to_erase = [run_id for run_id in run_ids if run_id not in spared_runs]
    if not to_erase:
        return {"erased": 0, "cases": [{"case_id": row["case_id"]} for row in spared]}
    try:
        removed = session.execute(_ERASE, {"run_ids": to_erase}).scalar_one()
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
        run_ids=to_erase,
    )
    return {
        "erased": int(removed),
        "cases": [{"case_id": row["case_id"]} for row in spared],
    }
