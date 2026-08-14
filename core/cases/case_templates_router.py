"""Case Templates API endpoints."""

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.cases.case_workflow_service import CaseWorkflowService
from core.routing import Auth, RouterMeta
from core.storage.schemas import CaseSchema, CaseTemplateSchema

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/cases/templates",
    tags=["case-templates"],
    auth=Auth.REQUIRED,
)
workflow_service = CaseWorkflowService()


class TemplateCreate(BaseModel):
    """Create template request."""

    name: str
    template_type: str
    description: Optional[str] = None
    default_priority: str = "medium"
    default_status: str = "open"
    default_sla_policy_id: Optional[str] = None
    task_templates: Optional[List[Dict]] = None
    playbook_steps: Optional[List[Dict]] = None
    applicable_mitre_techniques: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class TemplateUpdate(BaseModel):
    """Update template request."""

    name: Optional[str] = None
    description: Optional[str] = None
    default_priority: Optional[str] = None
    default_status: Optional[str] = None
    default_sla_policy_id: Optional[str] = None
    task_templates: Optional[List[Dict]] = None
    playbook_steps: Optional[List[Dict]] = None
    applicable_mitre_techniques: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    is_active: Optional[bool] = None


class CaseFromTemplate(BaseModel):
    """Create case from template."""

    title: str
    description: Optional[str] = None
    assignee: Optional[str] = None
    finding_ids: Optional[List[str]] = None
    override_priority: Optional[str] = None


@router.get("/")
async def list_templates(template_type: Optional[str] = None, active_only: bool = True):
    """
    List all case templates.

    Args:
        template_type: Filter by template type
        active_only: Only return active templates

    Returns:
        List of templates
    """
    templates = workflow_service.list_templates(template_type, active_only)
    return {"templates": CaseTemplateSchema.dump_many(templates)}


@router.get("/{template_id}")
async def get_template(template_id: str):
    """
    Get a specific template.

    Args:
        template_id: Template ID

    Returns:
        Template details
    """
    template = workflow_service.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return CaseTemplateSchema.dump(template)


@router.post("/")
async def create_template(data: TemplateCreate):
    """
    Create a new case template.

    Args:
        data: Template creation data

    Returns:
        Created template
    """
    template = workflow_service.create_template(
        name=data.name,
        template_type=data.template_type,
        description=data.description,
        default_priority=data.default_priority,
        default_status=data.default_status,
        default_sla_policy_id=data.default_sla_policy_id,
        task_templates=data.task_templates,
        playbook_steps=data.playbook_steps,
        applicable_mitre_techniques=data.applicable_mitre_techniques,
        tags=data.tags,
    )

    if not template:
        raise HTTPException(status_code=500, detail="Failed to create template")

    return CaseTemplateSchema.dump(template)


@router.put("/{template_id}")
async def update_template(template_id: str, data: TemplateUpdate):
    """
    Update a template.

    Args:
        template_id: Template ID
        data: Update data

    Returns:
        Success status
    """
    updates = {k: v for k, v in data.dict().items() if v is not None}
    success = workflow_service.update_template(template_id, updates)

    if not success:
        raise HTTPException(
            status_code=404, detail="Template not found or update failed"
        )

    return {"success": True}


@router.delete("/{template_id}")
async def delete_template(template_id: str):
    """
    Delete (deactivate) a template.

    Args:
        template_id: Template ID

    Returns:
        Success status
    """
    success = workflow_service.delete_template(template_id)

    if not success:
        raise HTTPException(status_code=404, detail="Template not found")

    return {"success": True}


@router.post("/{template_id}/create-case")
async def create_case_from_template(template_id: str, data: CaseFromTemplate):
    """
    Create a new case from a template.

    Args:
        template_id: Template ID
        data: Case creation data

    Returns:
        Created case
    """
    case = workflow_service.create_case_from_template(
        template_id=template_id,
        title=data.title,
        description=data.description,
        assignee=data.assignee,
        finding_ids=data.finding_ids,
        override_priority=data.override_priority,
    )

    if not case:
        raise HTTPException(
            status_code=500, detail="Failed to create case from template"
        )

    return CaseSchema.dump(case)
