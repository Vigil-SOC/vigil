"""An attached document, recorded on the case it was attached to.

One ``document`` evidence row per attachment: the original sits in the evidence
store with its hashes, and the row is named for it. A line in the case audit
log is what the Record tab shows.
"""

import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from core.cases.case_evidence_service import CaseEvidenceService
from core.storage.models import CaseAuditLog, CaseEvidence

NAME_MAX = 200


def _stored_name(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[-100:] or "document"
    return f"{uuid.uuid4().hex[:12]}-{safe}"


def attach_document(
    session: Session,
    case_id: str,
    source: Path,
    filename: str,
    *,
    name: Optional[str],
    pages: int,
    collected_by: str,
    store: Optional[CaseEvidenceService] = None,
) -> CaseEvidence:
    """Move ``source`` into the evidence store and record it. The caller owns the
    transaction; the stored copy is removed again if the row cannot be written."""
    store = store or CaseEvidenceService()
    label = (name or filename)[:NAME_MAX]
    relative = f"{case_id}/{_stored_name(filename)}"
    target = store.resolve_stored_file(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    try:
        evidence = store.add_evidence(
            case_id,
            "document",
            label,
            collected_by,
            description=f"{pages} page{'' if pages == 1 else 's'} read",
            file_path=relative,
            source="Attached to /hunt",
            session=session,
        )
        if evidence is None:
            raise RuntimeError("the evidence row could not be written")
        session.add(
            CaseAuditLog(
                entity_type="case",
                entity_id=case_id,
                action="attach",
                change_summary=f"Attached {label} to the hunt ({pages} page"
                f"{'' if pages == 1 else 's'})",
                changed_by=collected_by,
            )
        )
        session.flush()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return evidence
