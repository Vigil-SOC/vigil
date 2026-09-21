"""
Case Workflow Service - Manages case templates and workflow automation.

Handles template management, playbook execution, auto-assignment,
and task automation.
"""

import logging
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from core.cases.closure import ClosedByKind, ClosureCategory
from core.exceptions import NotFoundError, default_on_error
from core.storage.models import Case, CaseTask, CaseTemplate
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# unknown sits below low: a rated Case keeps its rating when merged with an
# unrated one, and two unrated Cases stay unknown. Names the ranker would
# also call unknown rank the same, so list.index cannot throw.
_MERGE_PRIORITY_RANK = {
    "unknown": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def preferred_merge_priority(target: str, source: str) -> str:
    """The higher-rated of two Case priorities. Unrecognized names rank as unknown."""
    if _MERGE_PRIORITY_RANK.get(source, 0) > _MERGE_PRIORITY_RANK.get(target, 0):
        return source
    return target


class CaseWorkflowService:
    """Service for managing case workflows and templates."""

    def __init__(self):
        """Initialize the workflow service."""

    @default_on_error(None)
    def create_template(
        self,
        name: str,
        template_type: str,
        description: Optional[str] = None,
        default_priority: str = "medium",
        default_status: str = "open",
        default_sla_policy_id: Optional[str] = None,
        task_templates: Optional[List[Dict]] = None,
        playbook_steps: Optional[List[Dict]] = None,
        applicable_mitre_techniques: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        session: Optional[Session] = None,
    ) -> Optional[CaseTemplate]:
        """
        Create a new case template.

        Args:
            name: Template name
            template_type: Type of template
            description: Template description
            default_priority: Default case priority
            default_status: Default case status
            default_sla_policy_id: Default SLA policy ID
            task_templates: List of task template dictionaries
            playbook_steps: List of playbook step dictionaries
            applicable_mitre_techniques: List of MITRE technique IDs
            tags: List of tags
            session: Database session (optional)

        Returns:
            Created CaseTemplate or None
        """
        with unit_of_work(session) as session:
            # Generate template ID
            timestamp = utcnow().strftime("%Y%m%d%H%M%S")
            template_id = f"template-{template_type}-{timestamp}"

            template = CaseTemplate(
                template_id=template_id,
                name=name,
                description=description,
                template_type=template_type,
                default_priority=default_priority,
                default_status=default_status,
                default_sla_policy_id=default_sla_policy_id,
                task_templates=task_templates or [],
                playbook_steps=playbook_steps or [],
                applicable_mitre_techniques=applicable_mitre_techniques or [],
                tags=tags or [],
                is_active=True,
                usage_count=0,
            )

            session.add(template)

            logger.info(f"Created case template: {template_id}")
            return template

    def get_template(
        self, template_id: str, session: Optional[Session] = None
    ) -> Optional[CaseTemplate]:
        """
        Get a case template by ID.

        Args:
            template_id: Template ID
            session: Database session (optional)

        Returns:
            CaseTemplate or None
        """
        with unit_of_work(session) as session:
            return (
                session.query(CaseTemplate)
                .filter(CaseTemplate.template_id == template_id)
                .first()
            )

    def list_templates(
        self,
        template_type: Optional[str] = None,
        active_only: bool = True,
        session: Optional[Session] = None,
    ) -> List[CaseTemplate]:
        """
        List case templates.

        Args:
            template_type: Filter by template type
            active_only: Only return active templates
            session: Database session (optional)

        Returns:
            List of CaseTemplate objects
        """
        with unit_of_work(session) as session:
            query = session.query(CaseTemplate)

            if template_type:
                query = query.filter(CaseTemplate.template_type == template_type)

            if active_only:
                query = query.filter(CaseTemplate.is_active.is_(True))

            return query.order_by(CaseTemplate.usage_count.desc()).all()

    @default_on_error(None)
    def create_case_from_template(
        self,
        template_id: str,
        title: str,
        description: Optional[str] = None,
        assignee: Optional[str] = None,
        finding_ids: Optional[List[str]] = None,
        override_priority: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> Optional[Case]:
        """
        Create a new case from a template.

        Args:
            template_id: Template ID
            title: Case title
            description: Case description
            assignee: Case assignee
            finding_ids: List of finding IDs to attach
            override_priority: Override template's default priority
            session: Database session (optional)

        Returns:
            Created Case or None
        """
        with unit_of_work(session) as session:
            # Get template
            template = (
                session.query(CaseTemplate)
                .filter(CaseTemplate.template_id == template_id)
                .first()
            )

            if not template or not template.is_active:
                logger.error(f"Template {template_id} not found or inactive")
                return None

            # Generate case ID
            timestamp = utcnow().strftime("%Y%m%d%H%M%S")
            case_id = f"case-{timestamp}"

            # Create case
            case = Case(
                case_id=case_id,
                title=title,
                description=description or template.description or "",
                priority=override_priority or template.default_priority,
                status=template.default_status,
                assignee=assignee,
                tags=template.tags.copy() if template.tags else [],
                mitre_techniques=(
                    template.applicable_mitre_techniques.copy()
                    if template.applicable_mitre_techniques
                    else []
                ),
                notes=[],
                timeline=[
                    {
                        "timestamp": utcnow().isoformat(),
                        "event": f"Case created from template: {template.name}",
                    }
                ],
                activities=[],
                resolution_steps=[],
            )

            session.add(case)
            session.flush()  # Flush to get case_id

            # Create tasks from template
            if template.task_templates:
                for task_tmpl in template.task_templates:
                    task = CaseTask(
                        case_id=case.case_id,
                        title=task_tmpl.get("title", ""),
                        description=task_tmpl.get("description", ""),
                        priority=task_tmpl.get("priority", "medium"),
                        status="pending",
                        task_order=task_tmpl.get("order", 0),
                        checklist_items=task_tmpl.get("checklist_items", []),
                    )
                    session.add(task)

            # Assign SLA if template has one
            if template.default_sla_policy_id:
                from core.cases.case_sla_service import CaseSLAService

                sla_service = CaseSLAService()
                sla_service.assign_sla_to_case(
                    case.case_id, template.default_sla_policy_id, session
                )

            # Increment template usage
            template.usage_count += 1

            # Attach findings if provided
            if finding_ids:
                from core.storage.models import Finding

                for finding_id in finding_ids:
                    finding = (
                        session.query(Finding)
                        .filter(Finding.finding_id == finding_id)
                        .first()
                    )
                    if finding:
                        case.findings.append(finding)

            logger.info(f"Created case {case_id} from template {template_id}")
            return case

    @default_on_error(False)
    def escalate_case(
        self,
        case_id: str,
        escalated_from: str,
        escalated_to: str,
        reason: str,
        urgency_level: str = "high",
        session: Optional[Session] = None,
    ) -> bool:
        """
        Escalate a case.

        Args:
            case_id: Case ID
            escalated_from: Who escalated the case
            escalated_to: Who to escalate to
            reason: Escalation reason
            urgency_level: Urgency level
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            from core.storage.models import CaseEscalation

            case = session.query(Case).filter(Case.case_id == case_id).first()
            if not case:
                logger.error(f"Case {case_id} not found")
                return False

            # Create escalation record
            escalation = CaseEscalation(
                case_id=case_id,
                escalated_from=escalated_from,
                escalated_to=escalated_to,
                reason=reason,
                urgency_level=urgency_level,
                status="pending",
            )
            session.add(escalation)

            # Update case
            case.assignee = escalated_to
            case.timeline.append(
                {
                    "timestamp": utcnow().isoformat(),
                    "event": f"Escalated to {escalated_to}: {reason}",
                }
            )

            logger.info(f"Escalated case {case_id} to {escalated_to}")
            return True

    @default_on_error(False)
    def update_template(
        self, template_id: str, updates: Dict, session: Optional[Session] = None
    ) -> bool:
        """
        Update a case template.

        Args:
            template_id: Template ID
            updates: Dictionary of fields to update
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            template = (
                session.query(CaseTemplate)
                .filter(CaseTemplate.template_id == template_id)
                .first()
            )

            if not template:
                logger.error(f"Template {template_id} not found")
                return False

            # Update allowed fields
            allowed_fields = [
                "name",
                "description",
                "default_priority",
                "default_status",
                "default_sla_policy_id",
                "task_templates",
                "playbook_steps",
                "applicable_mitre_techniques",
                "tags",
                "is_active",
            ]

            for field, value in updates.items():
                if field in allowed_fields and hasattr(template, field):
                    setattr(template, field, value)

            logger.info(f"Updated template {template_id}")
            return True

    @default_on_error(False)
    def delete_template(
        self, template_id: str, session: Optional[Session] = None
    ) -> bool:
        """
        Delete (deactivate) a case template.

        Args:
            template_id: Template ID
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            template = (
                session.query(CaseTemplate)
                .filter(CaseTemplate.template_id == template_id)
                .first()
            )

            if not template:
                logger.error(f"Template {template_id} not found")
                return False

            # Soft delete by deactivating
            template.is_active = False

            logger.info(f"Deactivated template {template_id}")
            return True

    def close_case(
        self,
        session: Session,
        case_id: str,
        *,
        closure_category: ClosureCategory,
        closed_by: str,
        closed_by_kind: ClosedByKind = ClosedByKind.AGENT,
        root_cause: Optional[str] = None,
        lessons_learned: Optional[str] = None,
        recommendations: Optional[str] = None,
        executive_summary: Optional[str] = None,
        false_positive_reason: Optional[str] = None,
        closure_notes: Optional[str] = None,
    ):
        """Mark a case closed and record its closure metadata.

        Returns the ``CaseClosureInfo`` row, or None if the case is unknown.

        The one place a Case's closure is written. Every path that closes a Case
        comes through here -- the two API endpoints, both MCP tools and a merge
        -- because a second writer that set some of these is a second definition
        of what a closure is, and because closing also stops the SLA resolution
        clock and indexes the Case's IOCs. Episodic memory reads these rows as
        Verdicts (#733), so a path that skipped either would produce a Case that
        is closed differently from every other closed Case.

        ``closed_by_kind`` defaults to ``agent`` rather than being inferred:
        ``analyst`` is the highest-trust record the system produces, and only a
        caller with an authenticated person behind it can honestly claim it.

        **An unstated close never overwrites a standing determination.** A
        status edit closes with ``unspecified``, which is not a determination,
        so where one is standing it leaves the category, the closer and the
        write-up alone: otherwise a console status edit after a considered close
        would erase the analyst's determination, and an agent's would silently
        downgrade its Trust from ``analyst`` to ``agent``.

        Where none is standing -- a Case closing for the first time, or one
        whose reopen retracted what it had determined -- this close owns the
        row, closer included. Holding the closer back there records the
        *previous* closer: an agent's status edit after an analyst's reopened
        close would inherit Trust ``analyst``, and memory would carry a
        determination no person made.
        """
        from core.cases.case_sla_service import CaseSLAService
        from core.storage.models import CaseClosureInfo
        from core.storage.shared_ioc_repository import index_case_iocs_on_close

        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            return None

        category = ClosureCategory(closure_category)
        kind = ClosedByKind(closed_by_kind)

        case.status = "closed"

        # Updated in place rather than merged over: merging a fresh row nulls
        # every field this call did not state, so a re-close would erase the
        # root cause and lessons learned of the close before it.
        closure = session.get(CaseClosureInfo, case_id)
        if closure is None:
            closure = CaseClosureInfo(
                case_id=case_id,
                closure_category=category.value,
                closed_by=closed_by,
                closed_by_kind=kind.value,
            )
            session.add(closure)
        elif (
            category is not ClosureCategory.UNSPECIFIED
            or closure.closure_category == ClosureCategory.UNSPECIFIED.value
        ):
            if closure.closed_by_kind == ClosedByKind.ANALYST.value and (
                kind is ClosedByKind.AGENT
            ):
                logger.info(
                    "Case %s was closed by an analyst and is being re-closed by "
                    "an agent as %s; its Verdict's Trust drops to agent",
                    case_id,
                    category.value,
                )
            closure.closure_category = category.value
            closure.closed_by = closed_by
            closure.closed_by_kind = kind.value

        closure.closed_at = utcnow()
        # Stated fields only. None means this call had nothing to say about the
        # field, which is not the same as saying it is empty.
        for field, value in (
            ("root_cause", root_cause),
            ("lessons_learned", lessons_learned),
            ("recommendations", recommendations),
            ("executive_summary", executive_summary),
            ("false_positive_reason", false_positive_reason),
            ("closure_notes", closure_notes),
        ):
            if value is not None:
                setattr(closure, field, value)

        CaseSLAService().mark_resolution_complete(case_id, session)
        session.flush()
        index_case_iocs_on_close(session, case_id)
        return closure

    def reopen_case(self, session: Session, case_id: str) -> None:
        """Retract what closing the Case determined, keeping what it wrote.

        A Case is reopened because its determination was wrong or premature, so
        the determination goes and the post-incident write-up stays: root cause,
        lessons learned and the executive summary are work, not a verdict, and
        deleting them is a data loss nobody asked for.

        What must not survive is the category. Left standing, the next status
        edit -- which states no category of its own -- would close the Case back
        into the determination the reopen retracted, and episodic memory would
        re-derive the Verdict the analyst reopened the Case to overturn.
        """
        from core.storage.models import CaseClosureInfo

        closure = session.get(CaseClosureInfo, case_id)
        if closure is not None:
            closure.closure_category = ClosureCategory.UNSPECIFIED.value

    def merge_cases(
        self, target_case_id: str, source_case_id: str, merged_by: str = "system"
    ) -> Optional[int]:
        """Absorb ``source_case_id`` into ``target_case_id``.

        Moves findings, timeline, activities, IOCs, evidence, tasks, and
        comments across; closes the source and links it with a 'merged_into'
        relationship. Returns the number of findings moved, and raises
        ``NotFoundError`` naming whichever case is missing.

        Runs in its own transaction — the whole merge must land or none of it.
        """
        from core.storage.models import (
            CaseClosureInfo,
            CaseComment,
            CaseEvidence,
            CaseIOC,
            CaseRelationship,
        )

        with unit_of_work() as session:
            target = session.query(Case).filter_by(case_id=target_case_id).first()
            source = session.query(Case).filter_by(case_id=source_case_id).first()
            if not target:
                raise NotFoundError(f"Target case {target_case_id} not found")
            if not source:
                raise NotFoundError(f"Source case {source_case_id} not found")

            target_finding_ids = {f.finding_id for f in target.findings}
            moved_findings = 0
            for finding in list(source.findings):
                if finding.finding_id not in target_finding_ids:
                    target.findings.append(finding)
                    moved_findings += 1
                source.findings.remove(finding)

            target.timeline = (target.timeline or []) + (source.timeline or [])
            target.activities = (target.activities or []) + (source.activities or [])
            target.resolution_steps = (target.resolution_steps or []) + (
                source.resolution_steps or []
            )
            target.mitre_techniques = list(
                set((target.mitre_techniques or []) + (source.mitre_techniques or []))
            )
            target.tags = list(set((target.tags or []) + (source.tags or [])))

            now = utcnow()
            target.activities.append(
                {
                    "timestamp": now.isoformat() + "Z",
                    "activity_type": "case_merged",
                    "description": f"Merged case {source_case_id} into this case",
                    "details": {
                        "source_case_id": source_case_id,
                        "source_title": source.title,
                        "findings_moved": moved_findings,
                        "merged_by": merged_by,
                    },
                }
            )

            if target.priority and source.priority:
                target.priority = preferred_merge_priority(
                    target.priority, source.priority
                )

            # Reparent the source's child records onto the target. Tolerated
            # per-model, as before the extraction: a merge still completes if
            # one child table is unavailable.
            for model in (CaseIOC, CaseEvidence, CaseTask, CaseComment):
                try:
                    rows = session.query(model).filter_by(case_id=source_case_id).all()
                except Exception:
                    continue
                for row in rows:
                    row.case_id = target_case_id

            session.add(
                CaseRelationship(
                    case_id=source_case_id,
                    related_case_id=target_case_id,
                    relationship_type="merged_into",
                    created_by=merged_by,
                    notes=f"Case merged into {target_case_id}",
                )
            )

            # A merge closes the source, and what it concluded is that this
            # record is the same record as another one -- which is the
            # `duplicate` category, and the category that writes no Verdict.
            # Left unrecorded, the close reads to episodic memory as one with no
            # stated reason and mints an inconclusive Verdict for a Case that
            # concluded nothing, counting the target's determination twice.
            #
            # A source that already determined something keeps it. `duplicate`
            # is a stated category, so `close_case` would let it overwrite an
            # analyst's `false_positive` -- and being the category that writes
            # no Verdict, it would withdraw the one already written. A merge
            # relates two records; it does not overturn what one of them found.
            standing = session.get(CaseClosureInfo, source_case_id)
            stated = (standing.closure_category or "").strip() if standing else ""
            if stated not in ("", ClosureCategory.UNSPECIFIED.value):
                logger.info(
                    "Case %s closed as %s is being merged into %s; the merge "
                    "records the relationship and leaves the determination",
                    source_case_id,
                    stated,
                    target_case_id,
                )
            else:
                self.close_case(
                    session,
                    source_case_id,
                    closure_category=ClosureCategory.DUPLICATE,
                    closed_by=merged_by,
                    closed_by_kind=ClosedByKind.AGENT,
                    closure_notes=f"Merged into {target_case_id}",
                )

            source.description = (source.description or "") + (
                f"\n\n[MERGED] This case was merged into {target_case_id} "
                f"by {merged_by} on {now.isoformat()}Z"
            )
            return moved_findings
