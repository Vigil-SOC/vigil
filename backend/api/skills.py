"""Skills API — CRUD + AI-assisted generation for reusable SOC capabilities.

See Issue #82 (Skill Builder). Execution of skills is out of scope here and
will be added by a follow-up PR on top of the llm_worker ARQ pattern.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.schemas.skill import (  # noqa: E402
    SkillCreate,
    SkillGenerateRequest,
    SkillGenerateResponse,
    SkillResponse,
    SkillUpdate,
)
from services.skill_service import SkillService  # noqa: E402

router = APIRouter()
logger = logging.getLogger(__name__)


def _service() -> SkillService:
    return SkillService()


@router.post("/generate", response_model=SkillGenerateResponse)
async def generate_skill(request: SkillGenerateRequest):
    """Generate a skill draft from a natural-language description.

    Supports multi-turn clarification. If Claude asks a question, the client
    re-submits with the prior conversation_history plus user_response.
    """
    try:
        conversation_history = request.conversation_history or []
        if request.user_response:
            conversation_history.append(
                {"role": "user", "content": request.user_response}
            )

        result = await _service().generate_skill(
            description=request.description,
            category=request.category,
            conversation_history=conversation_history or None,
        )

        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Failed to generate skill"),
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating skill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=SkillResponse, status_code=201)
@router.post(
    "/", response_model=SkillResponse, status_code=201, include_in_schema=False
)
async def create_skill(data: SkillCreate):
    """Persist a new skill."""
    try:
        created = _service().create_skill(
            data=data.model_dump(exclude={"created_by"}),
            created_by=data.created_by,
        )
        return created
    except Exception as e:
        logger.error("Error creating skill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=list[SkillResponse])
@router.get("/", response_model=list[SkillResponse], include_in_schema=False)
async def list_skills(
    category: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
):
    """List skills, optionally filtered by category and is_active."""
    try:
        return _service().list_skills(category=category, is_active=is_active)
    except Exception as e:
        logger.error("Error listing skills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(skill_id: str):
    skill = _service().get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    return skill


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(skill_id: str, patch: SkillUpdate):
    updated = _service().update_skill(
        skill_id=skill_id,
        patch=patch.model_dump(exclude_unset=True),
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    return updated


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    ok = _service().delete_skill(skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    return {"success": True, "skill_id": skill_id}
