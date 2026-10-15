"""Where a case's runs part ways, found by comparing their accounts.

Nothing a run writes says it disagrees with another run, so this reads what
each one concluded and lines them up. Heuristic on purpose: two runs never
word the same claim the same way, so claims are paired by side and by the
identifiers they share past what the whole case names, never by their wording. A pair this misses is a
disagreement the case page does not flag; a pair it invents is one a reader
dismisses at a glance, which is why every one names the runs and items it
rests on.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from core.memory.entity_keys import text_entity_keys
from core.storage.schemas.case_account import (
    CaseAccount,
    Disagreement,
    Explanation,
    Gap,
    ItemRef,
)

# needs_review is a run that would not stand behind a dismissal, so it objects to one
# as an attack does.
OBJECTING = {"malicious", "suspicious", "needs_review"}
CLEARED = {"benign", "false_positive"}
HELD = {"proven", "supported"}
DROPPED = {"ruled_out", "weakened"}

# The records that share the most with a gap; enough for a reader to open and judge.
RECORDS_PER_GAP = 3

# A token with a digit in it names something: FIN-WKS-014, FS-01, fin.7z, T1490.
# A bare number is a count or a time, and two runs share those by chance.
# Split on path separators too, so \\FS-01\Finance$ and "the FS-01 share" share FS-01.
_TOKEN = re.compile(r"[^\s\"'(),;:<>\[\]{}\\/$]+")
_NAMES = re.compile(r"[a-z].*\d|\d.*[a-z]")
_TRAILING = re.compile(r"[.,!?]+$")
_TECHNIQUE = re.compile(r"^t\d{4}(?:\.\d{3})?$")
# What every run mints for itself and means something different by: refs (r7, r7.0,
# tc_25), hypotheses (h1, h-98e38c67), records (ev-64769d746afe), graph nodes (N3, V5).
_RUN_LOCAL = re.compile(r"^(?:[a-z]|tc_)\d+(?:\.\d+)?$|^(?:h|ev|q|dec)-[0-9a-f]{6,}$")
# The pieces of an ISO time the tokenizer leaves: 2026-10-15t10, 00z.
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:t\d*)?$|^\d{2}(?:\.\d+)?z$")


def find_disagreements(accounts: Sequence[CaseAccount]) -> List[Disagreement]:
    """Every disagreement between ``accounts``, each one run's own account."""
    names = {
        run.run_id: f"{run.run_kind.replace('_', ' ')} {run.run_id[:8]}"
        for account in accounts
        for run in account.runs
    }
    claims = {
        (one.run_id, one.id): _about(one)
        for account in accounts
        for one in account.explanations or []
    }
    records = {
        (one.run_id, one.id): _identifiers(one.claim, one.entities or [])
        for account in accounts
        for one in account.evidence or []
    }
    common = _ubiquitous([*claims.values(), *records.values()])
    return [
        *_verdicts(accounts, names),
        *_standings(accounts, names, claims, common),
        *_gaps_answered(accounts, names, records, common),
    ]


def _identifiers(text: str, keys: Iterable[str] = ()) -> Set[str]:
    """What ``text`` names, lowercased: its entities, and every token naming something."""
    found = {key.split(":", 1)[1].lower() for key in [*text_entity_keys(text), *keys]}
    for token in _TOKEN.findall(text.lower()):
        token = _TRAILING.sub("", token)
        if _TECHNIQUE.match(token) or (
            _NAMES.search(token)
            and not _RUN_LOCAL.match(token)
            and not _TIME.match(token)
        ):
            found.add(token)
    return found


# A case is about its host, so its host is in most of the case's claims and records
# and sharing it ties nothing to anything. Named by more than half of them, an
# identifier says what the case is, not what one item is about.
def _ubiquitous(named: Sequence[Set[str]]) -> Set[str]:
    counts = Counter(one for each in named for one in each)
    return {one for one, count in counts.items() if count * 2 > len(named)}


def _verdicts(
    accounts: Sequence[CaseAccount], names: Dict[str, str]
) -> List[Disagreement]:
    verdicts = [account.verdict for account in accounts if account.verdict]
    objecting = [one for one in verdicts if one.disposition in OBJECTING]
    cleared = [one for one in verdicts if one.disposition in CLEARED]
    if not objecting or not cleared:
        return []
    said = "; ".join(
        f"{names[one.run_id]} says {one.disposition}" for one in objecting + cleared
    )
    return [
        Disagreement(
            kind="verdict",
            text=f"The runs do not agree on a dismissal: {said}.",
            runs=[one.run_id for one in objecting + cleared],
        )
    ]


# What a claim is about, for pairing it with another run's. Read from the sentence
# only to pair it, never stored as its subjects.
def _about(explanation: Explanation) -> Set[str]:
    return _identifiers(explanation.claim, explanation.subjects or []) | {
        one.lower() for one in explanation.techniques or []
    }


# The claims on one side are alternatives, so sharing a side is not being the same
# claim: two ruled the same way only when, past what the whole case names, they still
# name the same thing. One disagreement per held claim, with every run that dropped it.
def _standings(
    accounts: Sequence[CaseAccount],
    names: Dict[str, str],
    claims: Dict[Tuple[str, str], Set[str]],
    common: Set[str],
) -> List[Disagreement]:
    explanations = [one for account in accounts for one in account.explanations or []]
    found = []
    for held in (one for one in explanations if one.standing in HELD):
        about = claims[(held.run_id, held.id)] - common
        dropped = [
            one
            for one in explanations
            if one.standing in DROPPED
            and one.run_id != held.run_id
            and one.role == held.role
            and about & claims[(one.run_id, one.id)]
        ]
        if not dropped:
            continue
        against = "; ".join(
            f'{names[one.run_id]} has "{one.claim}" {one.standing.replace("_", " ")}'
            for one in dropped
        )
        found.append(
            Disagreement(
                kind="standing",
                text=(
                    f'{names[held.run_id]} holds "{held.claim}" ({held.standing}); '
                    f"{against}."
                ),
                runs=list(
                    dict.fromkeys([held.run_id, *(one.run_id for one in dropped)])
                ),
                refs=[
                    ItemRef(run_id=one.run_id, id=one.id) for one in [held, *dropped]
                ],
            )
        )
    return found


def _gaps_answered(
    accounts: Sequence[CaseAccount],
    names: Dict[str, str],
    records: Dict[Tuple[str, str], Set[str]],
    common: Set[str],
) -> List[Disagreement]:
    evidence = [one for account in accounts for one in account.evidence or []]
    found = []
    for gap in _unsettled(accounts):
        asked = _identifiers(gap.text) - common
        scored = [
            (len(asked & records[(item.run_id, item.id)]), item)
            for item in evidence
            if item.run_id != gap.run_id
        ]
        ranked = sorted(
            (pair for pair in scored if pair[0] > 0), key=lambda pair: -pair[0]
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
