"""Skills API — skills loaded from disk, and writes under the operator root.

Skills are directories of ``SKILL.md`` under the bundled library and the
operator root (``VIGIL_SKILLS_PATH``, else ``~/.vigil/skills``); see
``core.skills.skill_library``. Writes go only to that operator root. The
bundled library is never modified.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from core.auth import trail_events
from core.auth.current_user import get_current_user
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
from core.skills.skill_hold import (
    ApprovalRefused,
    ScanState,
    approve,
    digest_of,
    scan_states,
)
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
    managed_skills,
    operator_skills_root,
    read_skill_file,
    skill_body,
    skill_files,
    skill_roots,
    skill_version,
    skipped_note,
    write_operator_skill,
)
from core.skills.skill_scan import scan_mode
from core.skills.skill_usage import skill_usage
from core.storage.config_service import audited_change, write_config_audit
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


class SkillSource(BaseModel):
    """The repo a skill was installed from, and how many came with it."""

    label: str
    ref: Optional[str] = None
    path: str
    commit: str
    skills: int


class SkillsManaged(BaseModel):
    """The operator root is under the declared directory: Vigil writes none of it."""

    directory: str
    revision: str


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
    # Set for a skill installed from a git repo.
    source: Optional[SkillSource] = None
    # Set on every row while VIGIL_SKILLS_PATH is under VIGIL_INTENT_DIR (#5070).
    managed: Optional[SkillsManaged] = None


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
    # The only problem is that the name is already installed: it can be scanned.
    installed: bool = False


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


class SkillRepoScanned(BaseModel):
    name: str
    folder: str
    # scanned | failed | unavailable | disabled | not_scanned. Only scanned has a verdict.
    status: str
    # The hold rule's call on the verdict; a held skill is a Finding.
    held: Optional[bool] = None
    recommendation: Optional[str] = None  # SAFE | CAUTION | DO_NOT_INSTALL
    score: Optional[int] = None
    max_severity: Optional[str] = None
    reasons: list[SkillScanFinding] = []
    error: Optional[str] = None
    finding_id: Optional[str] = None
    # False with a finding_id: this content was already raised by an earlier scan.
    finding_new: bool = False


class SkillRepoScan(BaseModel):
    label: str
    commit: str
    # How the verdicts were reached: "static" or "llm:<model>".
    scan_mode: str
    skills: list[SkillRepoScanned]


class SkillLatest(BaseModel):
    # The commit the skill's repo is at now; None when its branch or tag is gone.
    commit: Optional[str] = None


class SkillLibraryChange(BaseModel):
    name: str
    folder: str
    # What changed, a new skill's description, or "" for one gone from the repo.
    note: str
    # Edited in Vigil since it was installed: updating replaces those edits.
    edited: bool = False
    problem: Optional[str] = None


class SkillLibraryPreview(BaseModel):
    label: str
    ref: Optional[str] = None
    path: str
    commit: str
    # The repo's commit now; updating sends it back, and a repo that moved is refused.
    latest: str
    changed: list[SkillLibraryChange]
    new: list[SkillLibraryChange]
    gone: list[SkillLibraryChange]


class SkillLibraryUpdateRequest(BaseModel):
    commit: str = Field(..., pattern=r"^[0-9a-f]{40,64}$")
    take: list[str] = Field(..., min_length=1, max_length=100)
    # Picks the preview said were edited here, so their edits may be replaced.
    edited: list[str] = Field(default_factory=list, max_length=100)


class SkillLibraryUpdated(BaseModel):
    skills: list[SkillResponse]
    removed: list[str]


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


def _sources(names: List[str]) -> Dict[str, dict]:
    """Where each repo-installed skill came from, or empty when the database
    cannot answer: like usage, it must not stop a list served from disk."""
    try:
        return skill_sources.sources(names)
    except Exception:  # noqa: BLE001
        logger.exception("skill install records unreadable; listing without them")
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
    sources: Optional[Dict[str, dict]] = None,
    managed: Any = ...,  # read here unless the caller read it once for many rows
) -> SkillResponse:
    reads, agents = (usage or {}).get(skill.name, (0, 0))
    source = (sources or {}).get(skill.name)
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        source_path=str(skill.path),
        bundled=is_bundled(skill),
        file_count=len(skill_files(skill)),
        reads_7d=reads,
        agents_7d=agents,
        scan=SkillScanSummary(**_scan_summary(state)),
        source=SkillSource(**source) if source else None,
        managed=_managed() if managed is ... else managed,
    )


def _managed() -> Optional[SkillsManaged]:
    managed = managed_skills()
    return SkillsManaged(**vars(managed)) if managed else None


def _state_of(skill: Skill) -> ScanState:
    return scan_states([skill])[skill.name]


def _loaded(name: str) -> Skill:
    skill = {item.name: item for item in load_skills(skill_roots())}.get(name)
    if skill is None:
        raise SkillNotFound(f"No skill named {name!r}")
    return skill


def _skill_record(skill: Skill) -> Dict[str, str]:
    """What the trail keeps of a skill on disk: its name, description and content
    digest. The digest's first 16 characters: the trail redacts a longer hex run."""
    return {
        "name": skill.name,
        "description": skill.description,
        "digest": digest_of(skill.path)[:16],
    }


def _current(names: List[str]) -> Dict[str, Dict[str, str]]:
    """``_skill_record`` of each of ``names`` loaded now; a missing one is left out."""
    loaded = {item.name: item for item in load_skills(skill_roots())}
    return {n: _skill_record(loaded[n]) for n in names if n in loaded}


def _audit_skills(
    session, user, key: str, action: str, old, new: Optional[Dict]
) -> None:
    write_config_audit(
        session,
        config_type="skill",
        config_key=key,
        action=action,
        old_value=old or None,
        new_value=new,
        changed_by=str(user.user_id),
    )


def _library_record(name: str) -> Dict:
    """Where the library of ``name`` came from; the commit shortened, as the
    trail redacts a full one."""
    got = dict(skill_sources.sources([name]).get(name, {}))
    if got.get("commit"):
        got["commit"] = got["commit"][:12]
    return {"name": name, **got}


def _install_audited(user, key: str, install) -> List[Skill]:
    """Run ``install`` (which writes skills nothing had), then record what it
    wrote. Recorded after, not before, since the names are known only then
    and a clone must not hold the trail's lock; a refused entry takes the
    written skills out again."""
    installed: List[Skill] = []
    try:
        with unit_of_work() as session:
            installed = install()
            _audit_skills(
                session,
                user,
                key,
                "create",
                None,
                {"skills": [_skill_record(s) for s in installed]},
            )
    except BaseException:
        for skill in installed:
            try:
                delete_operator_skill(skill.name)
                skill_sources.forget(skill.name)
            except Exception:  # noqa: BLE001
                logger.exception("could not take out unrecorded skill %s", skill.name)
        raise
    return installed


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
    sources = await asyncio.to_thread(_sources, [skill.name for skill in skills])
    managed = _managed()
    return [_response(s, states[s.name], usage, sources, managed) for s in skills]


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
    sources = await asyncio.to_thread(_sources, [skill.name])
    listed = _response(skill, state, _usage_by_skill(), sources, _managed())
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
async def save_skill(req: SkillWriteRequest, user=Depends(get_current_user)):
    """Write ``<root>/[<category>/...]<name>/SKILL.md``, bumping its version.

    An existing operator skill is overwritten when ``version`` is the one on disk
    (409 otherwise); ``source`` copies that skill's folder.
    """

    def write() -> Skill:
        old = _current([req.name]).get(req.name)
        new = {
            "name": req.name,
            "description": req.description,
            "body": req.body,
            "source": req.source,
        }
        # Recorded first: a refused entry writes nothing, a refused write records nothing.
        with audited_change(
            str(user.user_id),
            config_type="skill",
            config_key=req.name,
            action="update" if old else "create",
            old_value=old,
            new_value=new,
        ):
            return write_operator_skill(
                req.name,
                req.description,
                req.body,
                source=req.source,
                expected_version=req.version,
            )

    try:
        skill = await asyncio.to_thread(write)
    except SkillError as exc:
        raise _http(exc) from exc
    skill_sweep.request()
    return _response(skill, await asyncio.to_thread(_state_of, skill))


@router.post("/upload", response_model=SkillUploadResponse, dependencies=_SKILLS_WRITE)
async def upload_skill(file: UploadFile = File(...), user=Depends(get_current_user)):
    """Install a ``SKILL.md`` or a ``.zip`` of one or more skills under the operator root.

    All or nothing: a skill refused refuses the upload, and a name already
    taken is refused (409); nothing is overwritten. Skills are found where
    ``npx skills`` looks in a repo; any other ``SKILL.md`` is skipped and
    counted, not installed.
    """
    data = await file.read(UPLOAD_MAX + 1)  # one byte over is enough to refuse it
    try:
        skipped = 0

        def install() -> List[Skill]:
            nonlocal skipped
            got, skipped = install_uploaded_skills(file.filename or "", data)
            return got

        installed = await asyncio.to_thread(
            _install_audited, user, f"upload:{file.filename or ''}", install
        )
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
            _install_audited,
            user,
            f"repo:{req.source}@{req.commit}",
            lambda: skill_sources.install_from_repo(
                req.source, req.commit, req.names, user.principal_id
            ),
        )
    except SkillError as exc:
        raise _http(exc) from exc
    skill_sweep.request()
    states = await asyncio.to_thread(scan_states, installed)
    skills = [_response(s, states[s.name]) for s in installed]
    return SkillUploadResponse(**skills[0].model_dump(), skills=skills, skipped=0)


@router.post("/repo/scan", response_model=SkillRepoScan, dependencies=_SKILLS_WRITE)
async def scan_repo(req: SkillRepoInstallRequest):
    """Scan the picked skills of a git repo at the commit the preview read, installing nothing.

    Each skill the hold rule holds is filed as a Finding (data_source
    ``skill-scan``); a rescan of the same content files no second one. The scan
    runs to the end, Findings included, even if the client stops waiting. 409
    when the repo has moved since the preview.
    """
    try:
        source, commit, scanned = await asyncio.to_thread(
            skill_sources.scan_repo, req.source, req.commit, req.names
        )
    except SkillError as exc:
        raise _http(exc) from exc
    return SkillRepoScan(
        label=source.label,
        commit=commit,
        scan_mode=scan_mode(),
        skills=[SkillRepoScanned(**vars(s)) for s in scanned],
    )


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
async def remove_skill(name: str, user=Depends(get_current_user)):
    """Delete an operator skill directory. A bundled skill is refused."""

    def delete() -> None:
        with audited_change(
            str(user.user_id),
            config_type="skill",
            config_key=name,
            action="delete",
            old_value=_current([name]).get(name) or {"name": name},
            new_value=None,
        ):
            delete_operator_skill(name)

    try:
        await asyncio.to_thread(delete)
    except SkillError as exc:
        raise _http(exc) from exc
    try:
        await asyncio.to_thread(skill_sources.forget, name)
    except Exception:  # noqa: BLE001 - the skill is gone; a stale row only names it
        logger.exception("could not drop the install record of %s", name)
    return {"deleted": name}


@router.get(
    "/{name}/library/latest", response_model=SkillLatest, dependencies=_SKILLS_WRITE
)
async def library_latest(name: str):
    """The commit the repo of a repo-installed skill is at now, cloning nothing."""
    try:
        commit = await asyncio.to_thread(skill_sources.latest_commit, name)
    except SkillError as exc:
        raise _http(exc) from exc
    return SkillLatest(commit=commit)


@router.post(
    "/{name}/library/preview",
    response_model=SkillLibraryPreview,
    dependencies=_SKILLS_WRITE,
)
async def library_preview(name: str):
    """What updating the library ``name`` came with would change, changing nothing."""
    try:
        got = await asyncio.to_thread(skill_sources.library_preview, name)
    except SkillError as exc:
        raise _http(exc) from exc
    return SkillLibraryPreview(
        label=got.source.label,
        ref=got.source.ref,
        path=got.source.path,
        commit=got.commit,
        latest=got.latest,
        changed=[SkillLibraryChange(**vars(c)) for c in got.changed],
        new=[SkillLibraryChange(**vars(c)) for c in got.new],
        gone=[SkillLibraryChange(**vars(c)) for c in got.gone],
    )


@router.post("/{name}/library/update", response_model=SkillLibraryUpdated)
async def library_update(
    name: str,
    req: SkillLibraryUpdateRequest,
    user=Depends(require_permission("automation.write")),
):
    """Take the picked changes from the repo at the commit the preview read.

    409 when the repo has moved since, a pick is no longer offered, or a pick
    was edited here and not listed in ``edited``.
    """

    def update() -> Tuple[List[Skill], List[str]]:
        before = _current(req.take)
        skills, removed = skill_sources.update_library(
            name, req.commit, req.take, user.principal_id, req.edited
        )
        # After, since the clone must not hold the trail's lock. Written already;
        # a refused entry cannot undo it, so it is logged loudly.
        try:
            with unit_of_work() as session:
                _audit_skills(
                    session,
                    user,
                    f"library:{name}@{req.commit}",
                    "update",
                    {"skills": list(before.values())},
                    {
                        "skills": [_skill_record(s) for s in skills],
                        "removed": removed,
                    },
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "skill library %s updated by %s but not recorded on the audit trail",
                name,
                user.user_id,
            )
        return skills, removed

    try:
        skills, removed = await asyncio.to_thread(update)
    except SkillError as exc:
        raise _http(exc) from exc
    skill_sweep.request()
    states = await asyncio.to_thread(scan_states, skills)
    return SkillLibraryUpdated(
        skills=[_response(s, states[s.name]) for s in skills], removed=removed
    )


@router.delete("/{name}/library", dependencies=_SKILLS_WRITE)
async def remove_library(name: str, user=Depends(get_current_user)):
    """Delete every skill installed with ``name`` from its repo, and stop tracking it."""

    def delete() -> List[str]:
        with audited_change(
            str(user.user_id),
            config_type="skill",
            config_key=f"library:{name}",
            action="delete",
            old_value=_library_record(name),
            new_value=None,
        ):
            return skill_sources.delete_library(name)

    try:
        deleted = await asyncio.to_thread(delete)
    except SkillError as exc:
        raise _http(exc) from exc
    return {"deleted": deleted}


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
