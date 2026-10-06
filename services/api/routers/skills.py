"""Skills API — skills loaded from disk, and writes under the operator root.

Skills are directories of ``SKILL.md`` under the bundled library and the
optional ``VIGIL_SKILLS_PATH`` root; see ``core.skills.skill_library``. Writes
go only to that operator root. The bundled library is never modified.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.routing import Auth, RouterMeta
from core.skills.skill_library import (
    Skill,
    SkillConflict,
    SkillError,
    SkillNotFound,
    delete_operator_skill,
    is_bundled,
    load_skills,
    operator_skills_root,
    read_skill_file,
    skill_body,
    skill_files,
    skill_roots,
    skill_version,
    write_operator_skill,
)

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
    bundled: bool
    file_count: int


class SkillFile(BaseModel):
    path: str
    size: int


class SkillFileContent(BaseModel):
    path: str
    content: str


class SkillDetail(SkillResponse):
    body: str
    operator_root_set: bool
    version: int
    files: list[SkillFile]


class SkillWriteRequest(BaseModel):
    name: str
    description: str
    body: str
    # A loaded skill to copy in full when saving under a new name.
    source: Optional[str] = None
    # The version the drawer opened; an overwrite is refused if it has moved.
    version: Optional[int] = None


def _response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        source_path=str(skill.path),
        bundled=is_bundled(skill),
        file_count=len(skill_files(skill)),
    )


def _loaded(name: str) -> Skill:
    skill = {item.name: item for item in load_skills(skill_roots())}.get(name)
    if skill is None:
        raise SkillNotFound(f"No skill named {name!r}")
    return skill


def _http(exc: SkillError) -> HTTPException:
    status = (
        404
        if isinstance(exc, SkillNotFound)
        else 409 if isinstance(exc, SkillConflict) else 400
    )
    return HTTPException(status_code=status, detail=str(exc))


@router.get("", response_model=list[SkillResponse])
@router.get("/", response_model=list[SkillResponse], include_in_schema=False)
async def list_skills():
    """Every valid skill under the configured roots, bundled library first."""
    return [_response(skill) for skill in load_skills(skill_roots())]


@router.get("/{name}", response_model=SkillDetail)
async def get_skill(name: str):
    """One skill, including the Markdown body the drawer edits."""
    try:
        skill = _loaded(name)
    except SkillError as exc:
        raise _http(exc) from exc
    try:
        body = skill_body(skill)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    listed = _response(skill)
    return SkillDetail(
        **listed.model_dump(),
        body=body,
        operator_root_set=operator_skills_root() is not None,
        version=skill_version(skill),
        files=skill_files(skill),
    )


@router.get("/{name}/files/{path:path}", response_model=SkillFileContent)
async def get_skill_file(name: str, path: str):
    """One text file in the skill folder, read-only. Nothing is executed."""
    try:
        content = read_skill_file(_loaded(name), path)
    except SkillError as exc:
        raise _http(exc) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillFileContent(path=path, content=content)


@router.post("", response_model=SkillResponse)
@router.post("/", response_model=SkillResponse, include_in_schema=False)
async def save_skill(req: SkillWriteRequest):
    """Write ``<vigil_skills_path>/<name>/SKILL.md``, bumping its version.

    An existing operator skill is overwritten when ``version`` is the one on disk
    (409 otherwise); ``source`` copies that skill's folder.
    """
    try:
        skill = write_operator_skill(
            req.name,
            req.description,
            req.body,
            source=req.source,
            expected_version=req.version,
        )
    except SkillError as exc:
        raise _http(exc) from exc
    return _response(skill)


@router.delete("/{name}")
async def remove_skill(name: str):
    """Delete an operator skill directory. A bundled skill is refused."""
    try:
        delete_operator_skill(name)
    except SkillError as exc:
        raise _http(exc) from exc
    return {"deleted": name}
