"""Restrict a case, narrow or widen its reader list, or lift it (#4354).

Who may read a restricted case is ``core.auth.permissions``' rule; this module
only changes the list. The caller has already loaded the case through that rule
(a case it cannot read is a 404) and holds ``cases.write``.

- Restricting takes the whole list in one act, and any principal may do it.
- Removing names is open to any principal: it narrows.
- Adding names, or lifting the restriction, widens: a person acting directly.
- A service account is named only by itself, when it is the restricter.
- A list left empty is refused (decision D on #4352).

Each change is one trail entry naming the actor and the change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import principals
from core.auth.acting import acts_as_person, is_service_account
from core.auth.tool_principal import current_claims
from core.cases import case_trail
from core.cases.agent_closure import principal_for_username
from core.exceptions import SOCError
from core.integrations.mcp.surface import current_caller
from core.storage.models import Case, CaseTask, Principal, User
from core.time import utcnow

PERSON_MUST = "A person must do this: adding readers or lifting a restriction widens who can read the case"
PERSON_MUST_MERGE = "A person must do this: merging two restricted cases with different readers widens who can read each"
NAME_ONE = "Name at least one reader"


class ReadersRefused(SOCError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message, "READERS_REFUSED")
        self.status_code = status_code


@dataclass(frozen=True)
class Restricter:
    """Who is changing the list.

    ``principal_id`` is the person or service account acting (an agent passes
    None); ``for_person`` is the person an agent acts for.
    """

    principal_id: Optional[str]
    service_account: bool = False
    person_direct: bool = False
    for_person: Optional[str] = None


def http_restricter(session: Session, user: User) -> Restricter:
    """The signed-in caller of a case route. Only a person's own session, outside
    any run or MCP call, acts directly; a token or a service account does not."""
    direct = (
        acts_as_person(user, session)
        and current_claims() is None
        and current_caller() is None
    )
    return Restricter(
        principal_id=getattr(user, "principal_id", None),
        service_account=is_service_account(user),
        person_direct=direct,
    )


def _unique(names: Iterable[Optional[str]]) -> List[str]:
    seen: List[str] = []
    for name in names:
        text = (name or "").strip() if isinstance(name, str) else ""
        if text and text not in seen:
            seen.append(text)
    return seen


def default_readers(
    session: Session, case: Optional[Case], actor: Restricter
) -> List[str]:
    """The owner, the task assignees, the restricter when a person or service
    account, and the person an agent acts for. Usernames resolve exactly; one
    that does not resolve is dropped, never guessed."""
    names: List[Optional[str]] = []
    if case is not None:
        owners = [case.assignee] + list(
            session.scalars(
                select(CaseTask.assignee).where(
                    CaseTask.case_id == case.case_id, CaseTask.assignee.isnot(None)
                )
            )
        )
        names += [principal_for_username(session, o) for o in _unique(owners)]
    return _unique([*names, actor.principal_id, actor.for_person])


def _checked(
    session: Session, names: Sequence[str], actor: Restricter, *, drop: bool = False
) -> List[str]:
    """Principal ids of live people and service accounts. A name that can't be
    named is refused; with ``drop`` (the defaults) it is left out instead."""
    ids = _unique(names)
    rows = dict(
        session.execute(
            select(Principal.principal_id, Principal.kind).where(
                Principal.principal_id.in_(ids), Principal.deleted_at.is_(None)
            )
        ).all()
    )
    named: List[str] = []
    for principal_id in ids:
        kind = rows.get(principal_id)
        if kind not in (principals.PERSON, principals.SERVICE_ACCOUNT):
            reason = f"{principal_id} is not a person or service account"
        elif kind == principals.SERVICE_ACCOUNT and not (
            actor.service_account and principal_id == actor.principal_id
        ):
            reason = "A service account can be named only by itself, when it restricts the case"
        else:
            named.append(principal_id)
            continue
        if not drop:
            raise ReadersRefused(reason)
    return named


def _initial(
    session: Session,
    case: Optional[Case],
    readers: Optional[Sequence[str]],
    actor: Restricter,
) -> List[str]:
    """The list a restriction starts with: ``readers`` as given, else the defaults."""
    if readers is None:
        names = _checked(
            session, default_readers(session, case, actor), actor, drop=True
        )
    else:
        names = _checked(session, readers, actor)
    if not names:
        raise ReadersRefused(NAME_ONE)
    return names


def _stamp(case: Case, readers: Optional[List[str]]) -> None:
    case.reader_principal_ids = readers or None
    case.updated_at = utcnow()


def _drop_watchers(session: Session, case: Case) -> None:
    """Watchers the new list leaves unable to read the case go, each on the trail."""
    from core.cases.case_collaboration_service import remove_watchers_who_cannot_read

    remove_watchers_who_cannot_read(session, case)


def readers_for_new_case(
    session: Session, readers: Optional[Sequence[str]], actor: Restricter
) -> List[str]:
    """The list a case is created restricted with: ``readers``, else the defaults."""
    return _initial(session, None, readers, actor)


def restrict(
    session: Session,
    case: Case,
    readers: Optional[Sequence[str]],
    actor: Restricter,
) -> List[str]:
    """Restrict an open case to ``readers`` (the defaults when None)."""
    if case.reader_principal_ids or case.reader_team_ids:
        raise ReadersRefused(
            "This case is already restricted: add or remove readers instead", 409
        )
    names = _initial(session, case, readers, actor)
    _stamp(case, names)
    session.flush()
    case_trail.append_case(session, case_trail.RESTRICT, case.case_id, readers=names)
    _drop_watchers(session, case)
    return names


def change_readers(
    session: Session,
    case: Case,
    actor: Restricter,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> List[str]:
    """Add and remove names on a restricted case. Adding is a person's own act."""
    current = list(case.reader_principal_ids or [])
    if not current and not case.reader_team_ids:
        raise ReadersRefused("This case is open: restrict it first", 409)
    removed = [n for n in _unique(remove) if n in current]
    added = [n for n in _unique(add) if n not in current]
    if added:
        if not actor.person_direct:
            raise ReadersRefused(PERSON_MUST, 403)
        _checked(session, added, actor)
    result = [n for n in current if n not in removed] + added
    if not result and not case.reader_team_ids:
        raise ReadersRefused(NAME_ONE)
    if not added and not removed:
        return result
    _stamp(case, result)
    session.flush()
    if added:
        case_trail.append_case(
            session, case_trail.READERS_ADD, case.case_id, added=added
        )
    if removed:
        case_trail.append_case(
            session, case_trail.READERS_REMOVE, case.case_id, removed=removed
        )
        _drop_watchers(session, case)
    return result


def lift(session: Session, case: Case, actor: Restricter) -> None:
    """Open a restricted case to everyone again: a person's own act."""
    before = list(case.reader_principal_ids or [])
    if not before and not case.reader_team_ids:
        raise ReadersRefused("This case is not restricted", 409)
    if not actor.person_direct:
        raise ReadersRefused(PERSON_MUST, 403)
    _stamp(case, None)
    case.reader_team_ids = None
    session.flush()
    case_trail.append_case(session, case_trail.UNRESTRICT, case.case_id, readers=before)


def _owners(session: Session, case: Case) -> List[str]:
    """The case owner and every task assignee on it, as stored."""
    return _unique(
        [case.assignee]
        + list(
            session.scalars(
                select(CaseTask.assignee).where(
                    CaseTask.case_id == case.case_id, CaseTask.assignee.isnot(None)
                )
            )
        )
    )


def merge_readers(
    session: Session, target: Case, source: Case, actor: Optional[Restricter]
) -> None:
    """The readers a merge of ``source`` into ``target`` leaves on the target.

    - Both open: nothing changes.
    - Restricted with open: the result keeps the restricted side's readers,
      refused while the open side's owner or task assignees aren't on that list.
    - Both restricted, different lists: the union, and only a person's own act.

    Runs before anything moves, so a refusal leaves both cases as they were.
    """
    from core.auth.permissions import case_restricted
    from core.cases.assignee import owner_reads_case

    held, giving = case_restricted(target), case_restricted(source)
    if not (held or giving):
        return
    if held and giving:
        mine = list(target.reader_principal_ids or [])
        added = [n for n in source.reader_principal_ids or [] if n not in mine]
        teams = list(target.reader_team_ids or [])
        added_teams = [t for t in source.reader_team_ids or [] if t not in teams]
        if not added and not added_teams:
            return
        if actor is None or not actor.person_direct:
            raise ReadersRefused(PERSON_MUST_MERGE, 403)
        _stamp(target, mine + added)
        target.reader_team_ids = (teams + added_teams) or None
        session.flush()
        case_trail.append_case(
            session,
            case_trail.READERS_ADD,
            target.case_id,
            added=added,
            merged_from=source.case_id,
        )
        return
    restricted, open_side = (target, source) if held else (source, target)
    stranded = [
        name
        for name in _owners(session, open_side)
        if not owner_reads_case(session, restricted, name)
    ]
    if stranded:
        raise ReadersRefused(
            f"{', '.join(stranded)} can't read restricted case {restricted.case_id}: "
            f"set Unassigned on {open_side.case_id} or add them as readers first"
        )
    if not held:
        readers = list(source.reader_principal_ids or [])
        _stamp(target, readers)
        target.reader_team_ids = list(source.reader_team_ids or []) or None
        session.flush()
        case_trail.append_case(
            session,
            case_trail.RESTRICT,
            target.case_id,
            readers=readers,
            merged_from=source.case_id,
        )
