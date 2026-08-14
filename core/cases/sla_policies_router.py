"""SLA Policies API endpoints."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import SLAPolicy
from core.storage.schemas import CaseSchema, SLAPolicySchema

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/sla-policies",
    tags=["sla-policies"],
    auth=Auth.REQUIRED,
)


class SLAPolicyCreate(BaseModel):
    """Create SLA policy request."""

    policy_id: str
    name: str
    description: Optional[str] = None
    priority_level: str  # critical, high, medium, low
    response_time_hours: float
    resolution_time_hours: float
    business_hours_only: bool = True
    escalation_rules: Optional[dict] = None
    notification_thresholds: Optional[List[int]] = None
    is_active: bool = True
    is_default: bool = False


class SLAPolicyUpdate(BaseModel):
    """Update SLA policy request."""

    name: Optional[str] = None
    description: Optional[str] = None
    response_time_hours: Optional[float] = None
    resolution_time_hours: Optional[float] = None
    business_hours_only: Optional[bool] = None
    escalation_rules: Optional[dict] = None
    notification_thresholds: Optional[List[int]] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


@router.get("/")
async def list_sla_policies(
    session: UnitOfWorkSession,
    active_only: bool = False,
    priority_level: Optional[str] = None,
    default_only: bool = False,
):
    """
    List all SLA policies.

    Args:
        active_only: Only return active policies
        priority_level: Filter by priority level
        default_only: Only return default policies

    Returns:
        List of SLA policies
    """
    query = session.query(SLAPolicy)

    if active_only:
        query = query.filter(SLAPolicy.is_active.is_(True))

    if priority_level:
        query = query.filter(SLAPolicy.priority_level == priority_level)

    if default_only:
        query = query.filter(SLAPolicy.is_default.is_(True))

    policies = query.all()

    return {"policies": SLAPolicySchema.dump_many(policies), "total": len(policies)}


@router.get("/{policy_id}")
async def get_sla_policy(policy_id: str, session: UnitOfWorkSession):
    """
    Get a specific SLA policy by ID.

    Args:
        policy_id: The policy ID

    Returns:
        SLA policy details
    """
    policy = session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()

    if not policy:
        raise HTTPException(status_code=404, detail="SLA policy not found")

    return SLAPolicySchema.dump(policy)


@router.post("/")
async def create_sla_policy(data: SLAPolicyCreate, session: UnitOfWorkSession):
    """
    Create a new SLA policy.

    Args:
        data: SLA policy creation data

    Returns:
        Created SLA policy
    """
    try:
        # Check if policy ID already exists
        existing = (
            session.query(SLAPolicy)
            .filter(SLAPolicy.policy_id == data.policy_id)
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Policy with ID {data.policy_id} already exists",
            )

        # Validate priority level
        valid_priorities = ["critical", "high", "medium", "low"]
        if data.priority_level not in valid_priorities:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid priority level. Must be one of: {valid_priorities}",
            )

        # Validate time values
        if data.response_time_hours <= 0:
            raise HTTPException(
                status_code=400, detail="Response time must be greater than 0"
            )

        if data.resolution_time_hours <= 0:
            raise HTTPException(
                status_code=400, detail="Resolution time must be greater than 0"
            )

        if data.response_time_hours >= data.resolution_time_hours:
            raise HTTPException(
                status_code=400,
                detail="Response time must be less than resolution time",
            )

        # If setting as default, unset other defaults for this priority
        if data.is_default:
            session.query(SLAPolicy).filter(
                SLAPolicy.priority_level == data.priority_level,
                SLAPolicy.is_default.is_(True),
            ).update({"is_default": False})

        # Create policy
        policy = SLAPolicy(
            policy_id=data.policy_id,
            name=data.name,
            description=data.description,
            priority_level=data.priority_level,
            response_time_hours=data.response_time_hours,
            resolution_time_hours=data.resolution_time_hours,
            business_hours_only=data.business_hours_only,
            escalation_rules=data.escalation_rules,
            notification_thresholds=data.notification_thresholds or [75, 90, 100],
            is_active=data.is_active,
            is_default=data.is_default,
        )

        session.add(policy)
        # Flush so the read-back sees server defaults; the request's
        # unit of work commits.
        session.flush()
        session.refresh(policy)

        return SLAPolicySchema.dump(policy)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create policy: {str(e)}"
        )


@router.put("/{policy_id}")
async def update_sla_policy(
    policy_id: str,
    data: SLAPolicyUpdate,
    session: UnitOfWorkSession,
):
    """
    Update an existing SLA policy.

    Args:
        policy_id: The policy ID
        data: SLA policy update data

    Returns:
        Updated SLA policy
    """
    try:
        policy = (
            session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()
        )

        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")

        # Update fields if provided
        if data.name is not None:
            policy.name = data.name

        if data.description is not None:
            policy.description = data.description

        if data.response_time_hours is not None:
            if data.response_time_hours <= 0:
                raise HTTPException(
                    status_code=400, detail="Response time must be greater than 0"
                )
            policy.response_time_hours = data.response_time_hours

        if data.resolution_time_hours is not None:
            if data.resolution_time_hours <= 0:
                raise HTTPException(
                    status_code=400, detail="Resolution time must be greater than 0"
                )
            policy.resolution_time_hours = data.resolution_time_hours

        # Validate response < resolution after updates
        if policy.response_time_hours >= policy.resolution_time_hours:
            raise HTTPException(
                status_code=400,
                detail="Response time must be less than resolution time",
            )

        if data.business_hours_only is not None:
            policy.business_hours_only = data.business_hours_only

        if data.escalation_rules is not None:
            policy.escalation_rules = data.escalation_rules

        if data.notification_thresholds is not None:
            policy.notification_thresholds = data.notification_thresholds

        if data.is_active is not None:
            policy.is_active = data.is_active

        if data.is_default is not None:
            # If setting as default, unset other defaults for this priority
            if data.is_default and not policy.is_default:
                session.query(SLAPolicy).filter(
                    SLAPolicy.priority_level == policy.priority_level,
                    SLAPolicy.policy_id != policy_id,
                    SLAPolicy.is_default.is_(True),
                ).update({"is_default": False})

            policy.is_default = data.is_default

        policy.updated_at = datetime.utcnow()
        # Flush so the read-back sees server defaults; the request's
        # unit of work commits.
        session.flush()
        session.refresh(policy)

        return SLAPolicySchema.dump(policy)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update policy: {str(e)}"
        )


@router.delete("/{policy_id}")
async def delete_sla_policy(
    policy_id: str,
    session: UnitOfWorkSession,
    force: bool = False,
):
    """
    Delete an SLA policy.

    Args:
        policy_id: The policy ID
        force: Force delete even if policy is in use

    Returns:
        Success message
    """
    try:
        policy = (
            session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()
        )

        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")

        # Check if policy is in use
        from core.storage.models import CaseSLA

        in_use = (
            session.query(CaseSLA).filter(CaseSLA.sla_policy_id == policy_id).count()
        )

        if in_use > 0 and not force:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete policy that is in use by {in_use} case(s). Use force=true to delete anyway.",
            )

        session.delete(policy)

        return {
            "success": True,
            "message": f"SLA policy {policy_id} deleted successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete policy: {str(e)}"
        )


@router.post("/{policy_id}/set-default")
async def set_default_policy(policy_id: str, session: UnitOfWorkSession):
    """
    Set a policy as the default for its priority level.

    Args:
        policy_id: The policy ID

    Returns:
        Updated policy
    """
    try:
        policy = (
            session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()
        )

        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")

        # Unset other defaults for this priority
        session.query(SLAPolicy).filter(
            SLAPolicy.priority_level == policy.priority_level,
            SLAPolicy.policy_id != policy_id,
            SLAPolicy.is_default.is_(True),
        ).update({"is_default": False})

        # Set this as default
        policy.is_default = True
        policy.updated_at = datetime.utcnow()
        # Flush so the read-back sees server defaults; the request's
        # unit of work commits.
        session.flush()
        session.refresh(policy)

        return SLAPolicySchema.dump(policy)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to set default policy: {str(e)}"
        )


@router.get("/{policy_id}/usage")
async def get_policy_usage(policy_id: str, session: UnitOfWorkSession):
    """
    Get usage statistics for an SLA policy.

    Args:
        policy_id: The policy ID

    Returns:
        Usage statistics
    """
    policy = session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()

    if not policy:
        raise HTTPException(status_code=404, detail="SLA policy not found")

    from core.storage.models import Case, CaseSLA

    # Total cases using this policy
    total_cases = (
        session.query(CaseSLA).filter(CaseSLA.sla_policy_id == policy_id).count()
    )

    # Active cases (not resolved)
    active_cases = (
        session.query(CaseSLA)
        .join(Case)
        .filter(
            CaseSLA.sla_policy_id == policy_id,
            Case.status.notin_(["resolved", "closed"]),
        )
        .count()
    )

    # Breached cases
    breached_cases = (
        session.query(CaseSLA)
        .filter(CaseSLA.sla_policy_id == policy_id, CaseSLA.breached.is_(True))
        .count()
    )

    # Compliance rate
    compliance_rate = 0.0
    if total_cases > 0:
        compliant_cases = total_cases - breached_cases
        compliance_rate = (compliant_cases / total_cases) * 100

    return {
        "policy_id": policy_id,
        "policy_name": policy.name,
        "total_cases": total_cases,
        "active_cases": active_cases,
        "breached_cases": breached_cases,
        "compliance_rate": round(compliance_rate, 2),
        "is_active": policy.is_active,
        "is_default": policy.is_default,
    }


@router.get("/{policy_id}/cases")
async def get_policy_cases(
    policy_id: str,
    session: UnitOfWorkSession,
    status: Optional[str] = None,
    breached_only: bool = False,
):
    """
    Get all cases using a specific SLA policy.

    Args:
        policy_id: The policy ID
        status: Filter by case status
        breached_only: Only return breached cases

    Returns:
        List of cases
    """
    policy = session.query(SLAPolicy).filter(SLAPolicy.policy_id == policy_id).first()

    if not policy:
        raise HTTPException(status_code=404, detail="SLA policy not found")

    from core.storage.models import Case, CaseSLA

    query = session.query(Case).join(CaseSLA).filter(CaseSLA.sla_policy_id == policy_id)

    if status:
        query = query.filter(Case.status == status)

    if breached_only:
        query = query.filter(CaseSLA.breached.is_(True))

    cases = query.all()

    return {
        "policy_id": policy_id,
        "cases": CaseSchema.dump_many(cases),
        "total": len(cases),
    }
