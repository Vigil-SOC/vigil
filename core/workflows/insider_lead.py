"""An insider lead: a finding that names an account, as a hunt on that account's
data movement (#4745).

The proposal is an execute body: one falsifiable hypothesis per ``user`` Entity
Key on the finding, that key its subject, on a restricted case. Nothing here
starts a hunt or asks the identity provider anything: the hunt does that.

The lead list (#4778) is one SystemConfig row, ``workflows.insider_leads``:
``{sources, rule_names, readers, teams}``. A finding is a lead when its
``data_source`` is in ``sources`` or its title is in ``rule_names``. No row, an
unreadable one, or no matchers is off: there is no separate switch. The readers
and teams are checked when a person writes the row, and are who read the cases
those leads open.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from core.memory.entity_keys import finding_entity_keys
from core.memory.hunt_coverage import EXECUTE_PATH, build_proposal

LEADS_KEY = "workflows.insider_leads"
PERSON_MUST_LEADS = (
    "A person must do this: adding a source, rule name, reader or team widens "
    "which findings start an insider hunt and who reads its cases"
)
NO_ACCOUNT = "names no account, so it is not an insider lead"


class LeadRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _claim(key: str, finding_id: str) -> str:
    # One line, whatever the key holds: subjects are keyed by the exact statement.
    return " ".join(
        f"Account {key} moved data beyond its own usual volume or destinations "
        f"(bulk download, external share, personal cloud or removable media) "
        f"in the days around finding {finding_id}".split()
    )


def account_keys(finding: Dict[str, Any]) -> List[str]:
    """The finding's ``user`` Entity Keys, in the order it names them."""
    return [key for key in finding_entity_keys([finding]) if key.startswith("user:")]


def lead_for(finding_id: str, finding: Dict[str, Any]) -> Dict[str, Any]:
    """The execute body for an already-read finding; ``LeadRefused`` 400 when it
    names no account."""
    keys = account_keys(finding)
    if not keys:
        raise LeadRefused(400, f"Finding {finding_id} {NO_ACCOUNT}")
    hypotheses: List[str] = []
    subjects: Dict[str, List[str]] = {}
    for key in keys:
        one = build_proposal([key], [], claim=_claim(key, finding_id))
        hypotheses.append(one["hypothesis"])
        subjects.update(one["hypothesis_subjects"])
    return {
        "execute_path": EXECUTE_PATH,
        "accounts": keys,
        "proposal": {
            "finding_id": finding_id,
            # execute_workflow reads each line as its own hypothesis.
            "hypothesis": "\n".join(hypotheses),
            "hypothesis_subjects": subjects,
            "approve_hypotheses": True,
            "restricted": True,
        },
    }


def lead_proposal(finding_id: str) -> Dict[str, Any]:
    """The execute body for ``finding_id``; ``LeadRefused`` when there is none."""
    from core.storage.database_data_service import get_database_data_service

    finding = get_database_data_service().get_finding(finding_id)
    if not finding:
        raise LeadRefused(404, f"Finding {finding_id} not found")
    return lead_for(finding_id, finding)


# ---------------------------------------------------------------------------
# The lead list
# ---------------------------------------------------------------------------


def _names(values: Any) -> List[str]:
    """Strings, stripped, blanks and duplicates (exact) dropped, order kept."""
    out: List[str] = []
    for value in values if isinstance(values, list) else []:
        text = value.strip() if isinstance(value, str) else ""
        if text and text not in out:
            out.append(text)
    return out


def _titles(values: Any) -> List[str]:
    """Rule names: as ``_names``, duplicates ignoring case."""
    out: List[str] = []
    for text in _names(values):
        if text.lower() not in {t.lower() for t in out}:
            out.append(text)
    return out


def leads() -> Dict[str, List[str]]:
    """The saved list; every field empty when there is no row or it can't be read."""
    from core.storage.config_service import get_config_service

    row = get_config_service().get_system_config(LEADS_KEY)
    row = row if isinstance(row, dict) else {}
    return {
        "sources": _names(row.get("sources")),
        "rule_names": _titles(row.get("rule_names")),
        "readers": _names(row.get("readers")),
        "teams": _names(row.get("teams")),
    }


def matches(finding: Dict[str, Any]) -> bool:
    """Whether the finding is an insider lead: its source is listed exactly, or its
    title is a listed rule name ignoring case."""
    row = leads()
    source, title = finding.get("data_source"), finding.get("title")
    if isinstance(source, str) and source in row["sources"]:
        return True
    return isinstance(title, str) and title.strip().lower() in {
        r.lower() for r in row["rule_names"]
    }


def configured_readers() -> Tuple[List[str], List[str]]:
    """The people and teams who read the cases leads open, as saved."""
    row = leads()
    return row["readers"], row["teams"]


def _live_teams(session: Any, team_ids: Sequence[str]) -> None:
    """A narrowing write keeps teams the person need not be allowed to name: each
    must still exist, unarchived, and not be the default team."""
    from sqlalchemy import select

    from core.cases import case_readers
    from core.storage.models import Team

    found = {
        t.team_id: t
        for t in session.scalars(select(Team).where(Team.team_id.in_(list(team_ids))))
    }
    for team_id in team_ids:
        team = found.get(team_id)
        if team is None or team.archived_at is not None:
            raise case_readers.ReadersRefused(f"{team_id} is not a team")
        if team.is_default:
            raise case_readers.ReadersRefused(case_readers.DEFAULT_TEAM)


def save_leads(
    session: Any,
    actor: Any,
    user_id: str,
    sources: Sequence[str],
    rule_names: Sequence[str],
    readers: Sequence[str],
    teams: Sequence[str],
) -> Dict[str, List[str]]:
    """Write the list as ``actor`` (a ``case_readers.Restricter``), audited as ``user_id``.

    Adding a matcher, reader or team that the saved row lacks widens, and is a
    person's own act. A list left on needs a reader or team, and every reader and
    team on it is checked, so a stale one is named and can be removed. Removing,
    or emptying the matchers, needs nothing more. Raises ``ReadersRefused`` (its
    ``status_code``), or ``LeadRefused`` 500 when the save fails.
    """
    from core.cases import case_readers
    from core.storage.config_service import get_config_service

    new = {
        "sources": _names(sources),
        "rule_names": _titles(rule_names),
        "readers": _names(readers),
        "teams": _names(teams),
    }
    old = leads()
    folded = {t.lower() for t in old["rule_names"]}
    widens = (
        any(s not in old["sources"] for s in new["sources"])
        or any(r.lower() not in folded for r in new["rule_names"])
        or any(r not in old["readers"] for r in new["readers"])
        or any(t not in old["teams"] for t in new["teams"])
    )
    if widens and not actor.person_direct:
        raise case_readers.ReadersRefused(PERSON_MUST_LEADS, 403)
    if new["sources"] or new["rule_names"]:
        if not (new["readers"] or new["teams"]):
            raise case_readers.ReadersRefused(case_readers.NAME_ONE)
        case_readers._checked(session, new["readers"], actor)
        if widens:
            case_readers._checked_teams(session, new["teams"], actor)
        else:
            _live_teams(session, new["teams"])
    saved = get_config_service(user_id=user_id).set_system_config(
        key=LEADS_KEY,
        value=new,
        description="Findings that start an insider hunt, and who reads its cases",
        config_type="workflows",
        change_reason=(
            f"Insider leads: {len(new['sources'])} sources, {len(new['rule_names'])} "
            f"rule names, {len(new['readers'])} readers, {len(new['teams'])} teams"
        ),
    )
    if not saved:
        raise LeadRefused(500, "Could not save the insider lead list")
    return new
