"""Skills API — skills loaded from disk, and writes under the operator root.

Skills are directories of ``SKILL.md`` under the bundled library and the
operator root (``VIGIL_SKILLS_PATH``, else ``~/.vigil/skills``); see
``core.skills.skill_library``. Writes go only to that operator root. The
bundled library is never modified.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from core.auth import trail_events
from core.auth.permissions import permission_gate, require_permission
from core.llm.cost.budget import BudgetExceeded
from core.llm.oneshot import NoProviderConfigured
from core.llm.providers.registry import get_registry
from core.llm.router.router import LLMRouter, get_provider_spec
from core.llm.security import PromptInjectionBlocked
from core.llm.target import component_fallback, model_for
from core.routing import Auth, RouterMeta
from core.skills import skill_sources
from core.skills.skill_ai_generator import SkillDraft, generate_skill_draft
from core.skills.skill_eval import has_cases, load_cases, run_cases
from core.skills.skill_hold import ApprovalRefused, ScanState, approve, scan_states
from core.skills.skill_library import (
    UPLOAD_MAX,
    Skill,
    SkillConflict,
    SkillError,
    SkillNotFound,
    delete_operator_skill,
    install_uploaded_skills,
    is_bundled,
    load_skills,
    operator_skills_root,
    read_skill_file,
    skill_body,
    skill_files,
    skill_roots,
    skill_version,
    skipped_note,
    write_operator_skill,
)
from core.skills.skill_usage import skill_usage
from core.storage.unit_of_work import unit_of_work
from services.api import skill_sweep
from services.api.routers.claude import NO_PROVIDER_DETAIL

logger = logging.getLogger(__name__)

router = APIRouter()

# A saved skill's description reaches every agent's system prompt, so writing
# one is automation.write. Running a skill's evals is too.
_SKILLS_WRITE = [permission_gate("automation.write")]
_SKILL_TEST = [permission_gate("automation.write")]
router.dependencies.append(permission_gate("automation.read"))

ROUTER_META = RouterMeta(
    prefix="/api/skills",
    tags=["skills"],
    auth=Auth.REQUIRED,
)


class SkillScanSummary(BaseModel):
    # bundled | unchecked | pending | failed | safe | flagged | approved | refused.
    # Only bundled, unchecked, safe and approved skills reach agents; the rest are
    # held. refused: the folder contains a symlink, which is never allowed.
    state: str
    held: bool
    recommendation: Optional[str] = None  # SAFE | CAUTION | DO_NOT_INSTALL
    score: Optional[int] = None


class SkillScanFinding(BaseModel):
    rule_id: str
    severity: str
    message: str
    file: Optional[str] = None


class SkillScanDetail(SkillScanSummary):
    max_severity: Optional[str] = None
    findings: list[SkillScanFinding] = []
    scanner_version: Optional[str] = None
    # How the verdict was reached: "static" or "llm:<model>".
    scan_mode: Optional[str] = None
    # The content the verdict is about; POST /{name}/approve takes it back.
    digest: Optional[str] = None
    scanned_at: Optional[datetime] = None
    error: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class SkillResponse(BaseModel):
    name: str
    description: str
    source_path: str
    bundled: bool
    file_count: int
    # Reads of the skill body in the last 7 days, and the distinct agents
    # that made them (#1560). Zero when the usage query could not run: the
    # skills themselves come from disk and must still list.
    reads_7d: int = 0
    agents_7d: int = 0
    scan: SkillScanSummary


class SkillApproveRequest(BaseModel):
    # The scan's digest the approver read; refused if the skill has moved on.
    digest: str


class SkillUploadResponse(SkillResponse):
    """The first skill installed, as a single upload always returned, plus all of them.

    A zip may hold several skills; ``skills`` lists every skill installed, and
    ``skipped`` counts the ``SKILL.md`` left out, which ``skipped_note``
    explains.
    """

    skills: List[SkillResponse]
    skipped: int = 0
    skipped_note: Optional[str] = None


class SkillFile(BaseModel):
    path: str
    size: int


class SkillFileContent(BaseModel):
    path: str
    content: str


class SkillDetail(SkillResponse):
    scan: SkillScanDetail
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

    # Write-time only: skills already on disk with an empty body still load.
    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Steps cannot be empty")
        return value


class SkillRepoRequest(BaseModel):
    # owner/repo, owner/repo/<folder>, a GitHub page link or an https git URL;
    # @<ref> picks a branch or tag.
    source: str = Field(..., min_length=1, max_length=500)


class SkillRepoFound(BaseModel):
    folder: str
    name: str
    description: str
    # Why it cannot be installed; None when it can.
    problem: Optional[str] = None


class SkillRepoPreview(BaseModel):
    label: str
    ref: Optional[str] = None
    path: str
    # The commit read; installing sends it back, and a repo that moved is refused.
    commit: str
    skills: list[SkillRepoFound]


class SkillRepoInstallRequest(SkillRepoRequest):
    commit: str = Field(..., pattern=r"^[0-9a-f]{40,64}$")
    names: list[str] = Field(..., min_length=1, max_length=100)


class SkillGenerateRequest(BaseModel):
    description: str = Field(..., pattern=r"\S")


class SkillGenerateResponse(BaseModel):
    draft: SkillDraft


class SkillCaseResult(BaseModel):
    name: str
    passed: bool
    # The expected strings the answer did not contain.
    missing: list[str]


class SkillTestResponse(BaseModel):
    # True when the skill ships no evals/cases.json; nothing was run.
    no_cases: bool
    model: Optional[str] = None
    results: list[SkillCaseResult]


def _usage_by_skill() -> Dict[str, Tuple[int, int]]:
    """The 7-day usage map, or empty when the database cannot answer.

    One grouped query for the whole list, joined in memory. A database
    error degrades to no usage rather than failing a list that is served
    from disk (#1560).
    """
    try:
        return skill_usage()
    except Exception:  # noqa: BLE001
        logger.exception("skill usage query failed; listing skills without it")
        return {}


def _scan_summary(state: ScanState) -> dict:
    return dict(
        state=state.state,
        held=not state.allowed,
        recommendation=state.recommendation,
        score=state.score,
    )


def _scan_detail(state: ScanState) -> SkillScanDetail:
    return SkillScanDetail(
        **_scan_summary(state),
        max_severity=state.max_severity,
        findings=[SkillScanFinding(**f) for f in state.findings],
        scanner_version=state.scanner_version,
        scan_mode=state.scan_mode,
        digest=state.digest,
        scanned_at=state.scanned_at,
        error=state.error,
        approved_by=state.approved_by,
        approved_at=state.approved_at,
    )


def _response(
    skill: Skill,
    state: ScanState,
    usage: Optional[Dict[str, Tuple[int, int]]] = None,
) -> SkillResponse:
    reads, agents = (usage or {}).get(skill.name, (0, 0))
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        source_path=str(skill.path),
        bundled=is_bundled(skill),
        file_count=len(skill_files(skill)),
        reads_7d=reads,
        agents_7d=agents,
        scan=SkillScanSummary(**_scan_summary(state)),
    )


def _state_of(skill: Skill) -> ScanState:
    return scan_states([skill])[skill.name]


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
    usage = _usage_by_skill()
    skills = load_skills(skill_roots())
    states = await asyncio.to_thread(scan_states, skills)
    return [_response(skill, states[skill.name], usage) for skill in skills]


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
        logger.warning("could not read skill %s: %s", name, exc)
        raise HTTPException(status_code=400, detail="Could not read the skill") from exc
    state = await asyncio.to_thread(_state_of, skill)
    listed = _response(skill, state, _usage_by_skill())
    return SkillDetail(
        **listed.model_dump(exclude={"scan"}),
        scan=_scan_detail(state),
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
    except OSError as exc:
        logger.warning("could not read skill file %s/%s: %s", name, path, exc)
        raise HTTPException(
            status_code=400, detail="Could not read the skill file"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillFileContent(path=path, content=content)


@router.post("", response_model=SkillResponse, dependencies=_SKILLS_WRITE)
@router.post(
    "/",
    response_model=SkillResponse,
    include_in_schema=False,
    dependencies=_SKILLS_WRITE,
)
async def save_skill(req: SkillWriteRequest):
    """Write ``<root>/[<category>/...]<name>/SKILL.md``, bumping its version.

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
    skill_sweep.request()
    return _response(skill, await asyncio.to_thread(_state_of, skill))


@router.post("/upload", response_model=SkillUploadResponse, dependencies=_SKILLS_WRITE)
async def upload_skill(file: UploadFile = File(...)):
    """Install a ``SKILL.md`` or a ``.zip`` of one or more skills under the operator root.

    All or nothing: a skill refused refuses the upload, and a name already
    taken is refused (409); nothing is overwritten. Skills are found where
    ``npx skills`` looks in a repo; any other ``SKILL.md`` is skipped and
    counted, not installed.
    """
    data = await file.read(UPLOAD_MAX + 1)  # one byte over is enough to refuse it
    try:
        installed, skipped = install_uploaded_skills(file.filename or "", data)
    except SkillError as exc:
        raise _http(exc) from exc
    skill_sweep.request()
    states = await asyncio.to_thread(scan_states, installed)
    skills = [_response(s, states[s.name]) for s in installed]
    return SkillUploadResponse(
        **skills[0].model_dump(),
        skills=skills,
        skipped=skipped,
        skipped_note=skipped_note(skipped),
    )


@router.post(
    "/repo/preview", response_model=SkillRepoPreview, dependencies=_SKILLS_WRITE
)
async def preview_repo(req: SkillRepoRequest):
    """Fetch a git repo and list the skills it holds, installing nothing.

    The host must be in ``VIGIL_SKILL_SOURCE_HOSTS``. Each skill that cannot be
    installed says why: it does not parse, or its name is taken.
    """
    try:
        source, commit, found = await asyncio.to_thread(
            skill_sources.preview, req.source
        )
    except SkillError as exc:
        raise _http(exc) from exc
    return SkillRepoPreview(
        label=source.label,
        ref=source.ref,
        path=source.path,
        commit=commit,
        skills=[SkillRepoFound(**vars(f)) for f in found],
    )


@router.post("/repo/install", response_model=SkillUploadResponse)
async def install_repo(
    req: SkillRepoInstallRequest,
    user=Depends(require_permission("automation.write")),
):
    """Install the picked skills from a git repo at the commit the preview read.

    As an upload: all or nothing, nothing overwritten, each scanned and held.
    409 when the repo has moved since the preview, or a name is taken.
    """
    try:
        installed = await asyncio.to_thread(
            skill_sources.install_from_repo,
            req.source,
            req.commit,
            req.names,
            user.principal_id,
        )
    except SkillError as exc:
        raise _http(exc) from exc
    skill_sweep.request()
    states = await asyncio.to_thread(scan_states, installed)
    skills = [_response(s, states[s.name]) for s in installed]
    return SkillUploadResponse(**skills[0].model_dump(), skills=skills, skipped=0)


@router.post(
    "/generate", response_model=SkillGenerateResponse, dependencies=_SKILLS_WRITE
)
async def generate_skill(req: SkillGenerateRequest):
    """Draft a skill from a description.

    Saves nothing: the drawer fills its form and POST /api/skills writes it.
    """
    try:
        draft = await generate_skill_draft(req.description)
    except SkillError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except NoProviderConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PromptInjectionBlocked as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BudgetExceeded:
        raise  # the app's handler renders it as a 402
    except Exception as exc:
        logger.exception("skill draft: model call failed")
        raise HTTPException(
            status_code=502,
            detail="The model call failed. Check the provider and the gateway logs.",
        ) from exc
    return SkillGenerateResponse(draft=draft)


@router.post("/{name}/approve", response_model=SkillResponse)
async def approve_skill(
    name: str,
    req: SkillApproveRequest,
    user=Depends(require_permission("skills.approve")),
):
    """Release a held skill's current content to agents.

    Needs ``skills.approve``, which ``automation.write`` does not carry, so the
    uploader cannot approve their own skill. 409 when the skill needs no
    approval, has no verdict yet, or its content is no longer ``digest``.
    """
    try:
        skill = _loaded(name)
    except SkillError as exc:
        raise _http(exc) from exc

    def release() -> ScanState:
        with unit_of_work() as session:
            try:
                before = approve(skill, req.digest, user.principal_id, session)
            except ApprovalRefused as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
            trail_events.record(
                session,
                "skills.approved",
                actor=user.principal_id,
                name=skill.name,
                digest=req.digest,
                state=before.state,
                recommendation=before.recommendation,
            )
        return _state_of(skill)

    # Digesting, the verdict read and the write are blocking: off the event loop.
    return _response(skill, await asyncio.to_thread(release))


@router.delete("/{name}", dependencies=_SKILLS_WRITE)
async def remove_skill(name: str):
    """Delete an operator skill directory. A bundled skill is refused."""
    try:
        delete_operator_skill(name)
    except SkillError as exc:
        raise _http(exc) from exc
    try:
        await asyncio.to_thread(skill_sources.forget, name)
    except Exception:  # noqa: BLE001 - the skill is gone; a stale row only names it
        logger.exception("could not drop the install record of %s", name)
    return {"deleted": name}


@router.post("/{name}/test", response_model=SkillTestResponse, dependencies=_SKILL_TEST)
async def test_skill(name: str):
    """Run the saved skill's ``evals/cases.json`` through the chat model.

    The model is the one assigned to ``chat_default``. A skill with no cases
    answers ``no_cases`` without touching a provider. A dispatch failure is an
    HTTP error, never a failed case.
    """
    try:
        skill = _loaded(name)
    except SkillError as exc:
        raise _http(exc) from exc

    # Running a held skill would put its content in front of a model, which is
    # what the hold exists to prevent.
    state = await asyncio.to_thread(_state_of, skill)
    if not state.allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Skill {name!r} is held ({state.state}) and cannot be tested until it is released.",
        )

    try:
        cases = load_cases(skill) if has_cases(skill) else []
    except (OSError, ValueError) as exc:
        logger.warning("skill %s: unreadable evals/cases.json: %s", name, exc)
        raise HTTPException(
            status_code=400, detail="evals/cases.json is not a valid list of cases"
        ) from exc
    if not cases:
        return SkillTestResponse(no_cases=True, results=[])

    resolved = get_registry().resolve_model_for_component("chat_default")
    provider = get_provider_spec(resolved[0]) if resolved else None
    if resolved is None or provider is None:
        raise HTTPException(status_code=503, detail=NO_PROVIDER_DETAIL["message"])
    model = model_for(
        provider, resolved[1], component_fallback("chat_default"), "chat_default"
    )

    try:
        results = await run_cases(
            LLMRouter(),
            provider,
            skill,
            max_tokens=1024,
            model=model,
            cases=cases,
        )
    except PromptInjectionBlocked as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BudgetExceeded:
        raise  # the app's handler renders it as a 402
    except Exception as exc:
        logger.exception("skill %s: test dispatch failed", name)
        raise HTTPException(
            status_code=502,
            detail=f"The model call to {provider.provider_id} failed. Check the provider and the gateway logs.",
        ) from exc
    return SkillTestResponse(
        no_cases=False,
        model=model,
        results=[
            SkillCaseResult(name=r.name, passed=r.passed, missing=r.missing)
            for r in results
        ],
    )
