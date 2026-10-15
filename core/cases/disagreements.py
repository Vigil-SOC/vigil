"""Where a case's runs part ways, found by comparing their accounts.

Nothing a run writes says it disagrees with another run, so this reads what
each one concluded and lines them up. Heuristic on purpose: two runs never
word the same claim the same way, so claims are paired by side and by the
identifiers they share, never by their wording. A pair this misses is a
disagreement the case page does not flag; a pair it invents is one a reader
dismisses at a glance, which is why every one names the runs and items it
rests on.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set

from core.memory.entity_keys import text_entity_keys
from core.storage.schemas.case_account import (
    CaseAccount,
    Disagreement,
    Explanation,
    Gap,
    ItemRef,
)

CONDEMNED = {"malicious", "suspicious"}
CLEARED = {"benign", "false_positive"}
HELD = {"proven", "supported"}
DROPPED = {"ruled_out", "weakened"}

# A case's own host is in nearly every record of every run, so one shared
# identifier ties a gap to everything. Two tie it to records about the same thing.
SHARED_FOR_A_GAP = 2
# The records that share the most with a gap; enough for a reader to open and judge.
RECORDS_PER_GAP = 3

# A token with a digit in it names something: FIN-WKS-014, FS-01, fin.7z, T1490.
# A bare number is a count or a time, and two runs share those by chance.
_TOKEN = re.compile(r"[^\s\"'(),;:<>\[\]{}]+")
_NAMES = re.compile(r"[a-z].*\d|\d.*[a-z]")
_TRAILING = re.compile(r"[.,!?]+$")


def find_disagreements(accounts: Sequence[CaseAccount]) -> List[Disagreement]:
    """Every disagreement between ``accounts``, each one run's own account."""
    names = {
        run.run_id: f"{run.run_kind.replace('_', ' ')} {run.run_id[:8]}"
        for account in accounts
        for run in account.runs
    }
    return [
        *_verdicts(accounts, names),
        *_standings(accounts, names),
        *_gaps_answered(accounts, names),
    ]


def _identifiers(text: str, keys: Iterable[str] = ()) -> Set[str]:
    """What ``text`` names, lowercased: its entities, and every token naming something."""
    found = {key.split(":", 1)[1].lower() for key in [*text_entity_keys(text), *keys]}
    for token in _TOKEN.findall(text.lower()):
        token = _TRAILING.sub("", token)
        if _NAMES.search(token):
            found.add(token)
    return found


def _verdicts(
    accounts: Sequence[CaseAccount], names: Dict[str, str]
) -> List[Disagreement]:
    verdicts = [account.verdict for account in accounts if account.verdict]
    condemned = [one for one in verdicts if one.disposition in CONDEMNED]
    cleared = [one for one in verdicts if one.disposition in CLEARED]
    if not condemned or not cleared:
        return []
    said = "; ".join(
        f"{names[one.run_id]} says {one.disposition}" for one in condemned + cleared
    )
    return [
        Disagreement(
            kind="verdict",
            text=f"The runs reach opposite verdicts: {said}.",
            runs=[one.run_id for one in condemned + cleared],
        )
    ]


# What a claim is about, for pairing it with another run's. Read from the sentence
# only to pair it, never stored as its subjects.
def _about(explanation: Explanation) -> Set[str]:
    return _identifiers(explanation.claim, explanation.subjects or []) | {
        one.lower() for one in explanation.techniques or []
    }


def _standings(
    accounts: Sequence[CaseAccount], names: Dict[str, str]
) -> List[Disagreement]:
    explanations = [one for account in accounts for one in account.explanations or []]
    found = []
    for held in (one for one in explanations if one.standing in HELD):
        for dropped in explanations:
            if (
                dropped.standing not in DROPPED
                or dropped.run_id == held.run_id
                or dropped.role != held.role
                or not _about(held) & _about(dropped)
            ):
                continue
            found.append(
                Disagreement(
                    kind="standing",
                    text=(
                        f'{names[held.run_id]} holds "{held.claim}" ({held.standing}); '
                        f'{names[dropped.run_id]} has "{dropped.claim}" '
                        f"{dropped.standing.replace('_', ' ')}."
                    ),
                    runs=[held.run_id, dropped.run_id],
                    refs=[
                        ItemRef(run_id=held.run_id, id=held.id),
                        ItemRef(run_id=dropped.run_id, id=dropped.id),
                    ],
                )
            )
    return found


def _gaps_answered(
    accounts: Sequence[CaseAccount], names: Dict[str, str]
) -> List[Disagreement]:
    evidence = [
        (item, _identifiers(item.claim, item.entities or []))
        for account in accounts
        for item in account.evidence or []
    ]
    found = []
    for gap in _unsettled(accounts):
        asked = _identifiers(gap.text)
        ranked = sorted(
            (
                (len(asked & named), item)
                for item, named in evidence
                if item.run_id != gap.run_id and len(asked & named) >= SHARED_FOR_A_GAP
            ),
            key=lambda scored: -scored[0],
        )
        answers = [item for _, item in ranked[:RECORDS_PER_GAP]]
        if not answers:
            continue
        holders = list(dict.fromkeys(item.run_id for item in answers))
        found.append(
            Disagreement(
                kind="gap_answered",
                text=(
                    f'{names[gap.run_id]} could not settle "{gap.text}"; '
                    f"{', '.join(names[one] for one in holders)} holds records on it."
                ),
                runs=[gap.run_id, *holders],
                refs=[ItemRef(run_id=item.run_id, id=item.id) for item in answers],
            )
        )
    return found


# A disputed tie is the run arguing with itself, not a question another run answers.
def _unsettled(accounts: Iterable[CaseAccount]) -> List[Gap]:
    return [
        gap
        for account in accounts
        for gap in account.gaps or []
        if gap.kind != "disputed"
    ]
