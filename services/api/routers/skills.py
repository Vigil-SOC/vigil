"""Skills API — the skills the agent can read, as loaded from disk (#882, #928).

Skills are directories of ``SKILL.md`` under the bundled library and the
optional ``VIGIL_SKILLS_PATH`` root; see ``core.skills.skill_library``. There
is no store behind this endpoint and nothing to write.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from core.routing import Auth, RouterMeta
from core.skills.skill_library import load_skills, skill_roots

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/skills",
    tags=["skills"],
    auth=Auth.REQUIRED,
)


class SkillResponse(BaseModel):
    name: str
    description: str
    source_path: str


@router.get("", response_model=list[SkillResponse])
@router.get("/", response_model=list[SkillResponse], include_in_schema=False)
async def list_skills():
    """Every valid skill under the configured roots, bundled library first."""
    return [
        SkillResponse(name=s.name, description=s.description, source_path=str(s.path))
        for s in load_skills(skill_roots())
    ]
