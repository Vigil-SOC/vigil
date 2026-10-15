"""A case's account: its runs' accounts side by side, merged on read.

Computed whenever the case is read rather than stored, so it can never be
stale and nothing has to invalidate it when a run ends. Every item keeps the
run it came from, and an item is only ever identified by ``(run_id, id)``: two
runs mint the same ids (``h1``, ``r7.0``), and an item's ``bears_on`` and a
timeline entry's ``ref`` resolve within its own run. Nothing here rewrites an
item but the case's verdict, which is the merge's own word when the runs do
not let any one of them speak for the case.

Which claims disagree is not settled here. A later check compares the runs;
this keeps every run's claim so there is something to compare.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from core.agents.projections import PROJECTION_READ_CONCURRENCY, read_account
from core.cases.case_records_service import list_case_runs
from core.storage.schemas.case_account import (
    ACCOUNT_SCHEMA_VERSION,
    CaseAccount,
    TimelineEntry,
    Verdict,
)

logger = logging.getLogger(__name__)

# A dismissal closes a case as nothing. It stands only when no run on the case
# leans the other way, nor was held for a person, nor could not be read: in a
# SOC the safe error is to escalate.
DISMISSALS = frozenset({"benign", "false_positive"})
OBJECTIONS = frozenset({"malicious", "suspicious", "needs_review"})

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
    """The case's runs, oldest first: the order the merge reads them in.

    By when each run ended, since a run's verdict is as new as its ending: a
    long hunt started first and finished last is the latest word. A run still
    going is placed by when it started.
    """
    runs = list_case_runs(session, case_id)
    runs.sort(key=lambda run: (run.finished_at or run.started_at, run.started_at))
    return [run.run_id for run in runs]


async def read_case_account(run_ids: Sequence[str]) -> CaseAccountRead:
    """Every run's account read, a few at a time, then merged in the order given.

    A run whose kind keeps no account is not on the case's account and is not
    unread either. One whose account could not be fetched or broke the
    contract is skipped and named, and the verdict is told it is missing.
    """
    gate = asyncio.Semaphore(PROJECTION_READ_CONCURRENCY)

    async def gated(run_id: str) -> Optional[CaseAccount]:
        async with gate:
            return await read_account(run_id)

    results = await asyncio.gather(
        *(gated(run_id) for run_id in run_ids), return_exceptions=True
    )
    accounts: List[CaseAccount] = []
    unread: List[str] = []
    for run_id, result in zip(run_ids, results):
        if isinstance(result, Exception):
            logger.warning("left run %s off its case's account: %s", run_id, result)
            unread.append(run_id)
        elif isinstance(result, BaseException):
            # A cancellation is not a run that could not be read.
            raise result
        elif result is not None:
            accounts.append(result)
    return CaseAccountRead(account=merge_accounts(accounts, unread), unread=unread)


def merge_accounts(
    accounts: Sequence[CaseAccount], unread: Sequence[str] = ()
) -> CaseAccount:
    """One account from several, given oldest first.

    The verdict is the case's (see ``case_verdict``); the narrative is the
    verdict's run's where it wrote one, else the latest; the chain is the
    latest. Every list keeps every run's items, in run order, with
    explanations grouped by role and the timeline in time order. A section no
    run filled stays absent; one some run filled is present, even empty.
    """
    merged: dict = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "runs": [run for account in accounts for run in account.runs],
    }
    newest = list(reversed(accounts))

    verdict = case_verdict(
        [account.verdict for account in accounts if account.verdict is not None],
        unread,
    )
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

    if _concat(accounts, "timeline") is not None:
        merged["timeline"] = _in_time_order(accounts)

    for name in LISTS:
        items = _concat(accounts, name)
        if items is not None:
            merged[name] = items

    # Only what some run filled is passed, so dump_account leaves the rest out.
    return CaseAccount(**merged)


def case_verdict(
    verdicts: Sequence[Verdict], unread: Sequence[str] = ()
) -> Optional[Verdict]:
    """The case's verdict from its runs', given oldest first.

    The latest run speaks, so nothing newer is hidden, except that:

    - a run that found an attack is not outranked by a later one that could not
      settle the case, only by a later attack or a dismissal;
    - a dismissal stands only if no run objects (malicious, suspicious or held
      for a person) and every run was read. Otherwise the case is held for a
      person, in the dismissing run's words, with the basis naming who objects.
    """
    if not verdicts:
        return None
    latest = verdicts[-1]
    if latest.disposition not in DISMISSALS:
        attack = next(
            (one for one in reversed(verdicts) if one.disposition == "malicious"), None
        )
        return attack or latest

    objecting = [one for one in verdicts if one.disposition in OBJECTIONS]
    if not objecting and not unread:
        return latest
    reasons = [f"run {one.run_id} says {one.disposition}" for one in objecting]
    reasons += [f"run {run_id} could not be read" for run_id in unread]
    return Verdict(
        run_id=latest.run_id,
        disposition="needs_review",
        headline=latest.headline,
        basis=f"Run {latest.run_id} says {latest.disposition}, but "
        + "; ".join(reasons)
        + ".",
    )


def _concat(accounts: Iterable[CaseAccount], name: str) -> Optional[list]:
    held = [
        getattr(account, name)
        for account in accounts
        if getattr(account, name) is not None
    ]
    return None if not held else [item for items in held for item in items]


def _instant(at: Optional[str]) -> Optional[datetime]:
    """An ``at`` as a UTC instant: an offset applied, a bare time read as UTC
    as the contract writes it, a date alone as its midnight. None when there
    is no time or it does not read as one."""
    if not at:
        return None
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        when.replace(tzinfo=timezone.utc)
        if when.tzinfo is None
        else when.astimezone(timezone.utc)
    )


def _in_time_order(accounts: Sequence[CaseAccount]) -> List[TimelineEntry]:
    """Every run's entries by time. An entry with no time sorts at the time its
    own run gave before it, else the next its run gives, so it stays beside its
    neighbours; a run that gives no time at all goes last, in its own order."""
    keyed: List[Tuple[Tuple[int, datetime], int, TimelineEntry]] = []
    never = datetime.max.replace(tzinfo=timezone.utc)
    for account in accounts:
        entries = account.timeline or []
        times = [_instant(entry.at) for entry in entries]
        for at, entry in enumerate(entries):
            before = next((one for one in reversed(times[: at + 1]) if one), None)
            after = next((one for one in times[at:] if one), None)
            when = before or after
            keyed.append(((0, when) if when else (1, never), len(keyed), entry))
    return [entry for _, _, entry in sorted(keyed, key=lambda one: (one[0], one[1]))]
