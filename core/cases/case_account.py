"""A case's account: its runs' accounts side by side, merged on read.

Computed whenever the case is read rather than stored, so it can never be
stale and nothing has to invalidate it when a run ends. Every item keeps the
run it came from, and an item is only ever identified by ``(run_id, id)``: two
runs mint the same ids (``h1``, ``r7.0``), and an item's ``bears_on`` and a
timeline entry's ``ref`` resolve within its own run. Nothing here rewrites an
item; the merge decides which run speaks for the single-valued sections and
in what order the rest are laid out.

Where runs disagree is not settled here. A later check compares them; this
keeps every run's claim so there is something to compare.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from core.agents.projections import read_account
from core.cases.case_records_service import list_case_runs
from core.storage.schemas.case_account import (
    ACCOUNT_SCHEMA_VERSION,
    CaseAccount,
    TimelineEntry,
)

logger = logging.getLogger(__name__)

# What a run can settle a case as. inconclusive and needs_review are a run that
# could not, and suspicious is one that would not; a later run that did outranks them.
DECISIVE = frozenset({"malicious", "benign", "false_positive"})

# The two sides a case is argued between, the attack first.
ROLE_ORDER = {"adversary": 0, "benign": 1}

LISTS = (
    "evidence",
    "accounts",
    "entities",
    "next_steps",
    "gaps",
    "escalations",
    "steps",
)


@dataclass
class CaseAccountRead:
    """The merged account, and the runs whose account could not be read.

    A run left out is named rather than dropped silently: a case read without
    one of its runs reads as though that run never said anything.
    """

    account: CaseAccount
    unread: List[str] = field(default_factory=list)


def case_run_ids(session: Session, case_id: str) -> List[str]:
    """The case's runs, oldest first: the order the merge reads them in."""
    return [run.run_id for run in reversed(list_case_runs(session, case_id))]


async def read_case_account(run_ids: Sequence[str]) -> CaseAccountRead:
    """Every run's account read concurrently, then merged, oldest run first.

    A run whose kind keeps no account is not on the case's account and is not
    unread either. One whose account could not be fetched or broke the
    contract is skipped and named.
    """
    results = await asyncio.gather(
        *(read_account(run_id) for run_id in run_ids), return_exceptions=True
    )
    accounts: List[CaseAccount] = []
    unread: List[str] = []
    for run_id, result in zip(run_ids, results):
        if isinstance(result, BaseException):
            logger.warning("left run %s off its case's account: %s", run_id, result)
            unread.append(run_id)
        elif result is not None:
            accounts.append(result)
    return CaseAccountRead(account=merge_accounts(accounts), unread=unread)


def merge_accounts(accounts: Sequence[CaseAccount]) -> CaseAccount:
    """One account from several, given oldest first.

    The verdict is the latest decisive run's, else the latest run's; the
    narrative is the verdict's run's where it wrote one, else the latest; the
    chain is the latest. Every list keeps every run's items, in run order,
    with explanations grouped by role and the timeline in time order. A section
    no run filled stays absent; one some run filled is present, even empty.
    """
    merged: dict = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "runs": [run for account in accounts for run in account.runs],
    }
    newest = list(reversed(accounts))

    verdicts = [account.verdict for account in newest if account.verdict is not None]
    verdict = next(
        (one for one in verdicts if one.disposition in DECISIVE), None
    ) or next(iter(verdicts), None)
    if verdict is not None:
        merged["verdict"] = verdict

    narratives = [
        account.narrative for account in newest if account.narrative is not None
    ]
    narrative = next(
        (
            one
            for one in narratives
            if verdict is not None and one.run_id == verdict.run_id
        ),
        None,
    )
    narrative = narrative or next(iter(narratives), None)
    if narrative is not None:
        merged["narrative"] = narrative

    chain = next(
        (account.chain for account in newest if account.chain is not None), None
    )
    if chain is not None:
        merged["chain"] = chain

    explanations = _concat(accounts, "explanations")
    if explanations is not None:
        # Stable, so each role keeps its runs in order.
        merged["explanations"] = sorted(
            explanations, key=lambda one: ROLE_ORDER[one.role]
        )

    timeline = _concat(accounts, "timeline")
    if timeline is not None:
        merged["timeline"] = _in_time_order(accounts)

    for name in LISTS:
        items = _concat(accounts, name)
        if items is not None:
            merged[name] = items

    # Only what some run filled is passed, so dump_account leaves the rest out.
    return CaseAccount(**merged)


def _concat(accounts: Iterable[CaseAccount], name: str) -> Optional[list]:
    held = [
        getattr(account, name)
        for account in accounts
        if getattr(account, name) is not None
    ]
    return None if not held else [item for items in held for item in items]


def _in_time_order(accounts: Sequence[CaseAccount]) -> List[TimelineEntry]:
    """Every run's entries by time. An entry with no time sorts at the last
    time its own run gave before it, so it stays beside its neighbours."""
    keyed: List[Tuple[str, int, TimelineEntry]] = []
    for account in accounts:
        last = ""
        for entry in account.timeline or []:
            last = entry.at or last
            keyed.append((last, len(keyed), entry))
    return [entry for _, _, entry in sorted(keyed, key=lambda one: (one[0], one[1]))]
