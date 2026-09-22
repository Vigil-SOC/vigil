"""
Case SLA Service - Manages SLA tracking and compliance.

Handles SLA calculation based on business hours, breach detection,
notifications, and reporting.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.exceptions import default_on_error
from core.storage.models import Case, CaseSLA, SLAPolicy
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)


class SlaOutcome(str, Enum):
    """Why an assignment ended the way it did.

    Assigning used to answer ``None`` to five different questions -- no such
    case, no such policy, a policy that was retired, no default for the
    priority, and anything that raised -- and its callers each guessed
    differently. One answered 500 to an operator naming a retired policy;
    another discarded the answer, so a case created from a template naming one
    was created with no SLA at all: no deadlines, no breach tracking, and a log
    line as the only trace.
    """

    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    NO_SUCH_CASE = "no_such_case"
    POLICY_NOT_FOUND = "policy_not_found"
    POLICY_RETIRED = "policy_retired"
    NO_DEFAULT_POLICY = "no_default_policy"


@dataclass(frozen=True)
class SlaAssignment:
    """What happened, and the row if there is one.

    Truthy exactly when a case has an SLA at the end of it, so a caller that
    only wants to know that still reads correctly.
    """

    outcome: SlaOutcome
    sla: Optional[CaseSLA] = None
    # The policy the caller named, so a refusal can say which one it means.
    policy_id: Optional[str] = None

    def __bool__(self) -> bool:
        return self.sla is not None


class BusinessHoursCalculator:
    """Calculates time deltas considering business hours."""

    def __init__(
        self,
        business_start_hour: int = 9,
        business_end_hour: int = 17,
        business_days: List[int] = None,  # 0=Monday, 6=Sunday
    ):
        """
        Initialize business hours calculator.

        Args:
            business_start_hour: Start of business day (24h format)
            business_end_hour: End of business day (24h format)
            business_days: List of business days (0-6, Monday-Sunday)
        """
        self.business_start_hour = business_start_hour
        self.business_end_hour = business_end_hour
        self.business_days = business_days or [0, 1, 2, 3, 4]  # Mon-Fri default
        self.business_hours_per_day = business_end_hour - business_start_hour

    def is_business_hours(self, dt: datetime) -> bool:
        """Check if datetime falls within business hours."""
        if dt.weekday() not in self.business_days:
            return False
        if dt.hour < self.business_start_hour or dt.hour >= self.business_end_hour:
            return False
        return True

    def add_business_hours(self, start_time: datetime, hours: float) -> datetime:
        """
        Add business hours to a datetime.

        Args:
            start_time: Starting datetime
            hours: Number of business hours to add

        Returns:
            Datetime after adding business hours
        """
        if hours <= 0:
            return start_time

        current = start_time
        remaining_hours = hours

        # Move to next business hour if starting outside business hours
        if not self.is_business_hours(current):
            current = self._next_business_hour(current)

        while remaining_hours > 0:
            # Calculate hours remaining in current business day
            hours_left_today = self.business_end_hour - current.hour

            if remaining_hours <= hours_left_today:
                # Can finish today
                current = current + timedelta(hours=remaining_hours)
                remaining_hours = 0
            else:
                # Move to end of business day and continue tomorrow
                remaining_hours -= hours_left_today
                current = current.replace(
                    hour=self.business_end_hour, minute=0, second=0
                )
                current = self._next_business_hour(current)

        return current

    def _next_business_hour(self, dt: datetime) -> datetime:
        """Get the next business hour after the given datetime."""
        current = dt

        # If within business day but after hours, move to next business day start
        if (
            current.weekday() in self.business_days
            and current.hour >= self.business_end_hour
        ):
            current = current + timedelta(days=1)
            current = current.replace(hour=self.business_start_hour, minute=0, second=0)

        # Keep advancing until we hit a business day
        while current.weekday() not in self.business_days:
            current = current + timedelta(days=1)

        # Set to business start hour if before it
        if current.hour < self.business_start_hour:
            current = current.replace(hour=self.business_start_hour, minute=0, second=0)

        return current


class CaseSLAService:
    """Service for managing case SLAs."""

    def __init__(self):
        """Initialize the SLA service."""
        self.business_hours_calc = BusinessHoursCalculator()

    def assign_sla_to_case(
        self,
        case_id: str,
        sla_policy_id: Optional[str] = None,
        session: Optional[Session] = None,
        *,
        fall_back_to_default: bool = False,
    ) -> SlaAssignment:
        """
        Assign an SLA policy to a case.

        Args:
            case_id: Case ID
            sla_policy_id: SLA policy ID (if None, uses default for priority)
            session: Database session (optional)
            fall_back_to_default: when the named policy cannot be used, assign
                the default for the case's priority instead of refusing. For a
                caller with nobody to tell -- a case being created from a
                template that names a retired policy, where refusing means a
                case with no deadlines and no breach tracking. A person naming
                a policy by hand is told instead.

        Returns:
            An ``SlaAssignment`` saying what happened, carrying the row when
            there is one.

        Not wrapped in ``default_on_error``: ``core/exceptions.py`` says not to
        use it where a caller needs to tell failure from a legitimately empty
        result, and that is this method exactly. A database that cannot be read
        is a defect and reaches the error handler as one.
        """
        with unit_of_work(session) as session:
            # Get case
            case = session.query(Case).filter(Case.case_id == case_id).first()
            if not case:
                logger.error(f"Case {case_id} not found")
                return SlaAssignment(SlaOutcome.NO_SUCH_CASE)

            # Check if SLA already assigned
            existing_sla = (
                session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()
            )
            if existing_sla:
                logger.warning(f"SLA already assigned to case {case_id}")
                return SlaAssignment(
                    SlaOutcome.ALREADY_ASSIGNED,
                    existing_sla,
                    existing_sla.sla_policy_id,
                )

            policy = None
            named_but_unusable: Optional[SlaOutcome] = None

            # Deactivating is what an operator is told to do with a policy they
            # cannot delete, and that promise only holds if a named policy is
            # checked the same way the default one is: a template carrying
            # `default_sla_policy_id` names it explicitly, so without the
            # is_active filter an inactive policy keeps being assigned to new
            # cases. Retired and missing are looked up separately so the answer
            # can say which it was -- they are the same to a WHERE clause and
            # very different to whoever asked.
            if sla_policy_id:
                named = (
                    session.query(SLAPolicy)
                    .filter(SLAPolicy.policy_id == sla_policy_id)
                    .first()
                )
                if named is None:
                    named_but_unusable = SlaOutcome.POLICY_NOT_FOUND
                elif not named.is_active:
                    named_but_unusable = SlaOutcome.POLICY_RETIRED
                else:
                    policy = named

                if named_but_unusable and not fall_back_to_default:
                    logger.error(
                        "SLA policy %s cannot be assigned to case %s: %s",
                        sla_policy_id,
                        case_id,
                        named_but_unusable.value,
                    )
                    return SlaAssignment(named_but_unusable, policy_id=sla_policy_id)

            if policy is None:
                # The default for the case's priority: the path a caller naming
                # nothing already takes, and the one a fallback lands on.
                policy = (
                    session.query(SLAPolicy)
                    .filter(
                        and_(
                            SLAPolicy.priority_level == case.priority,
                            SLAPolicy.is_default.is_(True),
                            SLAPolicy.is_active.is_(True),
                        )
                    )
                    .first()
                )
                if policy is not None and named_but_unusable:
                    logger.warning(
                        "Case %s named SLA policy %s (%s); assigning the "
                        "default for priority %s instead.",
                        case_id,
                        sla_policy_id,
                        named_but_unusable.value,
                        case.priority,
                    )

            if not policy:
                logger.error(f"No SLA policy found for case {case_id}")
                return SlaAssignment(
                    named_but_unusable or SlaOutcome.NO_DEFAULT_POLICY,
                    policy_id=sla_policy_id,
                )

            # Calculate deadlines
            case_created = case.created_at

            if policy.business_hours_only:
                response_due = self.business_hours_calc.add_business_hours(
                    case_created, policy.response_time_hours
                )
                resolution_due = self.business_hours_calc.add_business_hours(
                    case_created, policy.resolution_time_hours
                )
            else:
                response_due = case_created + timedelta(
                    hours=policy.response_time_hours
                )
                resolution_due = case_created + timedelta(
                    hours=policy.resolution_time_hours
                )

            # Create SLA record
            case_sla = CaseSLA(
                case_id=case_id,
                sla_policy_id=policy.policy_id,
                response_due=response_due,
                resolution_due=resolution_due,
                breached=False,
                is_paused=False,
                total_pause_duration=0,
            )

            session.add(case_sla)

            logger.info(
                f"SLA assigned to case {case_id}: "
                f"response_due={response_due}, resolution_due={resolution_due}"
            )

            return SlaAssignment(SlaOutcome.ASSIGNED, case_sla, policy.policy_id)

    def check_sla_breach(
        self,
        case_id: str,
        current_time: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if a case has breached its SLA.

        Args:
            case_id: Case ID
            current_time: Time to check against (defaults to now)
            session: Database session (optional)

        Returns:
            Tuple of (is_breached, breach_type) where breach_type is
            'response', 'resolution', or None
        """
        with unit_of_work(session) as session:
            if current_time is None:
                current_time = utcnow()

            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()

            if not case_sla:
                return False, None

            # Adjust current time for paused duration
            effective_time = current_time
            if case_sla.is_paused:
                # Don't count time while paused
                return False, None

            # Check response SLA
            if not case_sla.response_completed_at:
                if effective_time > case_sla.response_due:
                    return True, "response"

            # Check resolution SLA
            if not case_sla.resolution_completed_at:
                if effective_time > case_sla.resolution_due:
                    return True, "resolution"

            return False, None

    def get_sla_status(
        self, case_id: str, session: Optional[Session] = None
    ) -> Optional[Dict]:
        """
        Get comprehensive SLA status for a case.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            Dictionary with SLA status details
        """
        with unit_of_work(session) as session:
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()

            if not case_sla:
                return None

            current_time = utcnow()

            # Calculate time remaining
            response_remaining = None
            resolution_remaining = None
            response_percent_elapsed = 0.0
            resolution_percent_elapsed = 0.0

            if not case_sla.response_completed_at and not case_sla.is_paused:
                response_delta = case_sla.response_due - current_time
                response_remaining = max(0, response_delta.total_seconds())
                total_response_time = (
                    case_sla.response_due - case_sla.created_at
                ).total_seconds()
                elapsed_response_time = (
                    current_time - case_sla.created_at
                ).total_seconds()
                response_percent_elapsed = (
                    min(100.0, (elapsed_response_time / total_response_time) * 100)
                    if total_response_time > 0
                    else 0.0
                )

            if not case_sla.resolution_completed_at and not case_sla.is_paused:
                resolution_delta = case_sla.resolution_due - current_time
                resolution_remaining = max(0, resolution_delta.total_seconds())
                total_resolution_time = (
                    case_sla.resolution_due - case_sla.created_at
                ).total_seconds()
                elapsed_resolution_time = (
                    current_time - case_sla.created_at
                ).total_seconds()
                resolution_percent_elapsed = (
                    min(100.0, (elapsed_resolution_time / total_resolution_time) * 100)
                    if total_resolution_time > 0
                    else 0.0
                )

            # Determine status
            is_breached, breach_type = self.check_sla_breach(
                case_id, current_time, session
            )

            # Determine health status
            health_status = "healthy"
            if is_breached:
                health_status = "breached"
            elif response_percent_elapsed >= 90 or resolution_percent_elapsed >= 90:
                health_status = "critical"
            elif response_percent_elapsed >= 75 or resolution_percent_elapsed >= 75:
                health_status = "warning"

            return {
                "case_id": case_id,
                "sla_policy_id": case_sla.sla_policy_id,
                "response_due": case_sla.response_due.isoformat(),
                "resolution_due": case_sla.resolution_due.isoformat(),
                "response_remaining_seconds": response_remaining,
                "resolution_remaining_seconds": resolution_remaining,
                "response_percent_elapsed": response_percent_elapsed,
                "resolution_percent_elapsed": resolution_percent_elapsed,
                "response_completed": case_sla.response_completed_at is not None,
                "resolution_completed": case_sla.resolution_completed_at is not None,
                "response_sla_met": case_sla.response_sla_met,
                "resolution_sla_met": case_sla.resolution_sla_met,
                "is_breached": is_breached,
                "breach_type": breach_type,
                "is_paused": case_sla.is_paused,
                "health_status": health_status,
            }

    @default_on_error(False)
    def pause_sla(
        self,
        case_id: str,
        reason: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> bool:
        """
        Pause SLA timer for a case.

        Args:
            case_id: Case ID
            reason: Reason for pausing
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()

            if not case_sla:
                logger.error(f"No SLA found for case {case_id}")
                return False

            if case_sla.is_paused:
                logger.warning(f"SLA for case {case_id} is already paused")
                return True

            case_sla.is_paused = True
            case_sla.paused_at = utcnow()

            logger.info(f"SLA paused for case {case_id}: {reason}")
            return True

    @default_on_error(False)
    def resume_sla(self, case_id: str, session: Optional[Session] = None) -> bool:
        """
        Resume SLA timer for a case.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()

            if not case_sla:
                logger.error(f"No SLA found for case {case_id}")
                return False

            if not case_sla.is_paused:
                logger.warning(f"SLA for case {case_id} is not paused")
                return True

            # Calculate pause duration
            if case_sla.paused_at:
                pause_duration = (utcnow() - case_sla.paused_at).total_seconds()
                case_sla.total_pause_duration += int(pause_duration)

                # Extend deadlines by pause duration
                pause_delta = timedelta(seconds=pause_duration)
                case_sla.response_due += pause_delta
                case_sla.resolution_due += pause_delta

            case_sla.is_paused = False
            case_sla.resumed_at = utcnow()

            logger.info(f"SLA resumed for case {case_id}")
            return True

    @default_on_error(False)
    def mark_resolution_complete(
        self, case_id: str, session: Optional[Session] = None
    ) -> bool:
        """
        Mark that case resolution has been completed.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()

            if not case_sla:
                return False

            current_time = utcnow()
            case_sla.resolution_completed_at = current_time
            case_sla.resolution_sla_met = current_time <= case_sla.resolution_due

            logger.info(
                f"Resolution marked complete for case {case_id}, "
                f"SLA met: {case_sla.resolution_sla_met}"
            )
            return True

    def get_breached_cases(self, session: Optional[Session] = None) -> List[Dict]:
        """
        Get all cases that have breached their SLA.

        Args:
            session: Database session (optional)

        Returns:
            List of case dictionaries with SLA information
        """
        with unit_of_work(session) as session:
            current_time = utcnow()

            # Get all active SLAs
            slas = (
                session.query(CaseSLA)
                .filter(
                    and_(
                        CaseSLA.resolution_completed_at.is_(None),
                        CaseSLA.is_paused.is_(False),
                    )
                )
                .all()
            )

            breached_cases = []
            for sla in slas:
                is_breached, breach_type = self.check_sla_breach(
                    sla.case_id, current_time, session
                )
                if is_breached:
                    case = (
                        session.query(Case).filter(Case.case_id == sla.case_id).first()
                    )
                    if case:
                        breached_cases.append(
                            {
                                "case_id": case.case_id,
                                "title": case.title,
                                "priority": case.priority,
                                "status": case.status,
                                "breach_type": breach_type,
                                "response_due": sla.response_due.isoformat(),
                                "resolution_due": sla.resolution_due.isoformat(),
                            }
                        )

            return breached_cases

    def get_sla_compliance_report(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> Dict:
        """
        Generate SLA compliance report.

        Args:
            start_date: Report start date
            end_date: Report end date
            session: Database session (optional)

        Returns:
            Dictionary with compliance statistics
        """
        with unit_of_work(session) as session:
            query = session.query(CaseSLA)

            if start_date:
                query = query.filter(CaseSLA.created_at >= start_date)
            if end_date:
                query = query.filter(CaseSLA.created_at <= end_date)

            all_slas = query.all()

            total_cases = len(all_slas)
            response_met = sum(1 for s in all_slas if s.response_sla_met)
            response_completed = sum(
                1 for s in all_slas if s.response_completed_at is not None
            )
            resolution_met = sum(1 for s in all_slas if s.resolution_sla_met)
            resolution_completed = sum(
                1 for s in all_slas if s.resolution_completed_at is not None
            )

            return {
                "total_cases": total_cases,
                "response_sla_met": response_met,
                "response_sla_completed": response_completed,
                "response_compliance_rate": (
                    (response_met / response_completed * 100)
                    if response_completed > 0
                    else 0.0
                ),
                "resolution_sla_met": resolution_met,
                "resolution_sla_completed": resolution_completed,
                "resolution_compliance_rate": (
                    (resolution_met / resolution_completed * 100)
                    if resolution_completed > 0
                    else 0.0
                ),
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
            }
