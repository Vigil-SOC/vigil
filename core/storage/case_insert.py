"""The one insert every database case goes through.

It places the case on the default team and writes the creation entry. Callers
that already hold a session use it directly; ``DatabaseService.create_case``
opens one around it.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.storage import case_trail_hook
from core.storage.models import Case, Finding
from core.teams import TeamRefused, default_team
from core.time import utcnow


def insert_case(
    session: Session,
    *,
    case_id: str,
    title: str,
    description: str = "",
    status: str = "new",
    priority: str = "medium",
    assignee: Optional[str] = None,
    tags: Optional[list] = None,
    notes: Optional[list] = None,
    timeline: Optional[list] = None,
    activities: Optional[list] = None,
    resolution_steps: Optional[list] = None,
    mitre_techniques: Optional[list] = None,
    finding_ids: Optional[Sequence[str]] = None,
) -> Case:
    """Insert one case on the caller's session. Does not commit."""
    team = default_team(session)
    if team.archived_at is not None:
        raise TeamRefused(f"{team.name} is archived.", 409)
    now = utcnow()
    case = Case(
        case_id=case_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        assignee=assignee,
        team_id=team.team_id,
        tags=tags if tags is not None else [],
        notes=notes if notes is not None else [],
        timeline=(
            timeline
            if timeline is not None
            else [{"timestamp": now.isoformat() + "Z", "event": "Case created"}]
        ),
        activities=activities if activities is not None else [],
        resolution_steps=resolution_steps if resolution_steps is not None else [],
        mitre_techniques=mitre_techniques,
    )
    case.team = team
    session.add(case)
    session.flush()

    if finding_ids:
        findings = (
            session.execute(select(Finding).where(Finding.finding_id.in_(finding_ids)))
            .scalars()
            .all()
        )
        case.findings.extend(findings)
        session.flush()

    if writer := case_trail_hook.writer(session):
        writer.append_case(
            session,
            writer.CREATE,
            case_id,
            operator_text=title or None,
            status=case.status,
            priority=case.priority,
            assignee=writer.assignee_principal(session, case.assignee),
        )
    return case
