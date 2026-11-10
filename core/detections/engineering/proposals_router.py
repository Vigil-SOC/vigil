"""Detection engineering: proposals and their steps (#4157).

Every step is a person's or a tool's event on the proposal. A proposal is rejected, never
deleted. Nothing here writes to a SIEM.

Ported from 1.x's ``services/api/routers/detection_engineering.py``: its proposal routes
only. The program view, drafting with the model, and shadow and live are not here yet.
"""

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import deny_unless, missing_permission, permission_gate
from core.detections.engineering import proposals, siem_replay, sigma_text
from core.detections.overlay import Refused
from core.routing import Auth, RouterMeta
from core.storage.models import User
from core.storage.models.detection_proposal import InvalidProposalMove, ProposalState

router = APIRouter()
router.dependencies.append(permission_gate("detections.read"))

ROUTER_META = RouterMeta(
    prefix="/api/detection-engineering",
    tags=["detection-engineering"],
    auth=Auth.REQUIRED,
)

_WRITE = [permission_gate("detections.write")]
_READ_FINDINGS = "findings.read"
_READ_CASES = "cases.read"
LONGEST_WINDOW_DAYS = 365


def _require(user: User, *permissions: str) -> None:
    for permission in permissions:
        deny_unless(user.user_id, permission)


def _may_read_replay(user: User, proposal_id: str) -> None:
    """A replay reports SIEM events, and on a case's rule it reads that case's events."""
    _require(user, _READ_FINDINGS)
    if proposals.case_of(proposal_id):
        _require(user, _READ_CASES)


def _reads(user: User, permission: str) -> bool:
    return missing_permission(user.user_id, permission) is None


def _all_shown(user: User, views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reads_findings = _reads(user, _READ_FINDINGS)
    reads_cases = _reads(user, _READ_CASES)
    return [
        proposals.shown_to(view, reads_findings=reads_findings, reads_cases=reads_cases)
        for view in views
    ]


def _shown(user: User, view: Dict[str, Any]) -> Dict[str, Any]:
    return _all_shown(user, [view])[0]


@contextmanager
def _answers() -> Iterator[None]:
    """A refused step is the answer: it says what is needed, and nothing moved."""
    try:
        yield
    except proposals.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidProposalMove as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Refused as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc), **exc.detail}
        ) from exc


class PasteRequest(BaseModel):
    sigma: str = Field(min_length=1, max_length=100_000)
    title: Optional[str] = Field(default=None, max_length=300)
    technique: Optional[str] = Field(default=None, max_length=16)
    case_id: Optional[str] = Field(default=None, max_length=50)
    finding_id: Optional[str] = Field(default=None, max_length=50)


class FromRuleRequest(BaseModel):
    rule_id: int


class EditRequest(BaseModel):
    sigma: str = Field(min_length=1, max_length=100_000)
    reason: str = Field(default="", max_length=2000)


class ReplayRequest(BaseModel):
    # Events a person names as the attack, by SIEM: {"elastic": ["<_id>", ...]}.
    named: Optional[Dict[Literal["elastic", "wazuh", "splunk"], List[str]]] = None
    days: int = Field(default=siem_replay.WINDOW_DAYS, ge=1, le=LONGEST_WINDOW_DAYS)


class TuneRequest(BaseModel):
    field: str = Field(min_length=1, max_length=128)
    match: Literal["contains", "startswith", "endswith", "equals"] = "contains"
    values: List[str] = Field(min_length=1, max_length=50)
    reason: str = Field(max_length=2000)


class ConvertRequest(BaseModel):
    # The native copies a person pastes, by SIEM: splunk (YAML), elastic (TOML), wazuh (XML).
    pasted: Dict[Literal["splunk", "elastic", "wazuh"], str]
    accept_reason: str = Field(default="", max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


@router.get("/proposals")
def list_proposals(
    state: Optional[ProposalState] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    rows = proposals.listing(state.value if state else None)
    return {"proposals": _all_shown(current_user, rows)}


@router.post("/proposals", dependencies=_WRITE)
def paste_proposal(body: PasteRequest, current_user: User = Depends(get_current_user)):
    origin = {
        "source": "pasted",
        "case_id": body.case_id,
        "finding_id": body.finding_id,
    }
    if body.case_id:
        _require(current_user, _READ_CASES)
    if body.finding_id:
        _require(current_user, _READ_FINDINGS)
    with _answers():
        return _shown(
            current_user,
            proposals.create(
                sigma=body.sigma,
                actor=current_user.username,
                title=body.title,
                technique=body.technique,
                case_id=body.case_id,
                origin=origin,
            ),
        )


@router.post("/proposals/from-rule", dependencies=_WRITE)
def prove_rule(body: FromRuleRequest, current_user: User = Depends(get_current_user)):
    with _answers():
        return _shown(
            current_user, proposals.from_rule(body.rule_id, current_user.username)
        )


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, current_user: User = Depends(get_current_user)):
    with _answers():
        return _shown(current_user, proposals.get(proposal_id))


@router.post("/proposals/{proposal_id}/edit", dependencies=_WRITE)
def edit_proposal(
    proposal_id: str,
    body: EditRequest,
    current_user: User = Depends(get_current_user),
):
    with _answers():
        return _shown(
            current_user,
            proposals.edit(proposal_id, body.sigma, current_user.username, body.reason),
        )


@router.post("/proposals/{proposal_id}/lint", dependencies=_WRITE)
def lint_proposal(proposal_id: str, current_user: User = Depends(get_current_user)):
    with _answers():
        return _shown(current_user, proposals.lint(proposal_id, current_user.username))


@router.post("/proposals/{proposal_id}/replay", dependencies=_WRITE)
async def replay_proposal(
    proposal_id: str,
    body: ReplayRequest,
    current_user: User = Depends(get_current_user),
):
    with _answers():
        _may_read_replay(current_user, proposal_id)
        return _shown(
            current_user,
            await proposals.replay(
                proposal_id,
                current_user.username,
                named=(
                    {str(siem): ids for siem, ids in body.named.items()}
                    if body.named
                    else None
                ),
                days=body.days,
            ),
        )


@router.post("/proposals/{proposal_id}/tune", dependencies=_WRITE)
async def tune_proposal(
    proposal_id: str,
    body: TuneRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        exclusion = sigma_text.Exclusion(body.field, body.match, tuple(body.values))
    except sigma_text.NotARule as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)}) from exc
    with _answers():
        _may_read_replay(current_user, proposal_id)
        return _shown(
            current_user,
            await proposals.tune(
                proposal_id, exclusion, current_user.username, body.reason
            ),
        )


@router.post("/proposals/{proposal_id}/convert", dependencies=_WRITE)
def convert_proposal(
    proposal_id: str,
    body: ConvertRequest,
    current_user: User = Depends(get_current_user),
):
    with _answers():
        return _shown(
            current_user,
            proposals.convert(
                proposal_id,
                current_user.username,
                pasted={str(siem): text for siem, text in body.pasted.items()},
                accept_reason=body.accept_reason,
            ),
        )


@router.post("/proposals/{proposal_id}/reject", dependencies=_WRITE)
def reject_proposal(
    proposal_id: str,
    body: ReasonRequest,
    current_user: User = Depends(get_current_user),
):
    with _answers():
        return _shown(
            current_user,
            proposals.reject(proposal_id, current_user.username, body.reason),
        )
