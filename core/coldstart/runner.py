"""The async run: collect, build a baseline, assess the review window, judge.

Every step reads where the run stands and does what is left, so a run whose process
died resumes through ``start``. Nothing here closes or dismisses an alert.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Set, Tuple

from core.coldstart import collect, store
from core.coldstart.baseline import (
    Alert,
    Baseline,
    SignatureBaseline,
    assess,
    build_baseline,
    signature_for,
)
from core.coldstart.judge import build_prompt, parse_verdict
from core.coldstart.tickets import Ticket, outcomes_from
from core.federation import registry
from core.federation import store as federation_store
from core.federation.adapters._base import cursor_at
from core.llm import oneshot
from core.llm.target import resolve_dispatch
from core.storage.models.cold_start import TRANSITIONS, InvalidTransition, RunStatus
from core.time import utcnow

logger = logging.getLogger(__name__)

JUDGE_TIMEOUT_SECONDS = 120.0
# Thinking models spend output tokens before the reply, as in the daemon's triage.
JUDGE_MAX_TOKENS = 8192
MAX_PER_SIGNATURE = 3
# A provider failing this many times running is not coming back for the next alert,
# and each failure can cost the full timeout.
MAX_CONSECUTIVE_FAILURES = 3

NO_MODEL = "no model"
LEASE_RETRY_SECONDS = 0.25

# asyncio only weak-refs tasks; unheld ones get collected mid-run.
_tasks: Set[asyncio.Task] = set()


class SourceUnavailable(Exception):
    pass


class NothingToBuild(Exception):
    pass


@dataclass
class Build:
    baseline: Baseline
    as_of: datetime
    assessed: List[Dict[str, Any]]
    tickets_unlinked: int


def build_run(
    alerts: List[Alert],
    tickets: Iterable[Ticket],
    reviews: Iterable[Tuple[str, str]] = (),
    *,
    history_days: int,
    review_days: int,
    now: datetime,
) -> Build:
    """Baseline as of the review window's start and an assessment of every alert in it.

    The window ends at the newest alert, capped at now, not at now itself: an export
    from last week, or a source that has been quiet, must still have something to
    review. For a source read just now the two are the same moment.
    """
    newest = max((a.observed_at for a in alerts), default=now)
    as_of = min(now, newest) - timedelta(days=review_days)
    start = as_of - timedelta(days=history_days)
    outcomes, unlinked = outcomes_from(tickets, {a.alert_id: a for a in alerts})
    baseline = build_baseline(
        [a for a in alerts if a.observed_at >= start],
        # Every ticket from before as_of counts, however old: a real ticket outside the
        # alert window is the one that stops a clean-looking kind from closing.
        outcomes,
        as_of,
    )
    # A person's word overrides the history without rewriting it: it is not a ticket and
    # did not happen before as_of. Agreeing changes nothing, it only records the review.
    for signature, verdict in reviews:
        if verdict == "not_benign":
            baseline.signatures.setdefault(
                signature, SignatureBaseline(signature=signature)
            ).marked_not_benign = True
    assessed = []
    for alert in alerts:
        if alert.observed_at < as_of:
            continue
        result = assess(alert, baseline)
        assessed.append(
            {
                "alert_id": alert.alert_id,
                "in_review_window": True,
                "stage1": result.stage1.value,
                "rule": result.rule,
                "reasons": list(result.reasons),
            }
        )
    return Build(baseline, as_of, assessed, unlinked)


def rebuild(run_id: str, now: datetime) -> None:
    """Recompute the baseline and assessments from what the run holds. Reviews and LLM
    results are other columns and stay."""
    params = store.run_params(run_id)
    build = build_run(
        store.load_alerts(run_id),
        store.load_tickets(run_id),
        store.load_reviews(run_id),
        history_days=params.history_days,
        review_days=params.review_days,
        now=now,
    )
    store.save_build(
        run_id,
        baseline=build.baseline.to_dict(),
        as_of=build.as_of,
        assessed=build.assessed,
        tickets_unlinked=build.tickets_unlinked,
    )


def pick_for_judging(
    candidates: List[store.Candidate], limit: int
) -> List[store.Candidate]:
    """At most MAX_PER_SIGNATURE of each kind, most recent first, taking every kind's
    newest before any kind's second so the limit cuts breadth last."""
    by_signature: Dict[str, List[store.Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_signature[signature_for(candidate.alert)].append(candidate)
    ranked = []
    for rows in by_signature.values():
        rows.sort(key=lambda c: (c.alert.observed_at, c.alert.alert_id), reverse=True)
        ranked.extend(enumerate(rows[:MAX_PER_SIGNATURE]))
    ranked.sort(
        key=lambda r: (r[0], -r[1].alert.observed_at.timestamp(), r[1].alert.alert_id)
    )
    return [candidate for _, candidate in ranked[:limit]]


async def _judge(run_id: str) -> None:
    params = await asyncio.to_thread(store.run_params, run_id)
    chosen = pick_for_judging(
        await asyncio.to_thread(store.judge_candidates, run_id), params.judge_limit
    )
    target = await asyncio.to_thread(resolve_dispatch, "triage")
    model = f"{target[0].provider_id}/{target[1]}" if target else None

    done = sum(1 for c in chosen if c.judged)
    await asyncio.to_thread(
        store.set_progress, run_id, phase="judging", done=done, total=len(chosen)
    )
    skipped = None
    failures = 0
    failed = 0
    for candidate in chosen:
        if candidate.judged:
            continue
        alert = candidate.alert
        prompt = build_prompt(alert, candidate.description)
        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(
                oneshot.complete(
                    message=prompt, component="triage", max_tokens=JUDGE_MAX_TOKENS
                ),
                timeout=JUDGE_TIMEOUT_SECONDS,
            )
        except oneshot.NoProviderConfigured:
            skipped = NO_MODEL
            break
        except Exception as e:  # left unjudged, so a resume tries it again
            failures += 1
            failed += 1
            logger.warning("Cold start judge call failed: %s", type(e).__name__)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                skipped = f"model failing: {type(e).__name__}"
                break
            continue
        failures = 0
        llm = {
            **parse_verdict(reply).as_dict(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "model": model,
        }
        await asyncio.to_thread(store.save_llm, run_id, alert.alert_id, llm)
        done += 1
        await asyncio.to_thread(store.set_progress, run_id, done=done)
    await asyncio.to_thread(
        store.set_progress, run_id, phase="done", judge_failed=failed
    )
    await asyncio.to_thread(
        store.transition, run_id, RunStatus.READY, judging_skipped=skipped
    )


async def _collect_from_adapter(run_id: str, source: str, now: datetime) -> None:
    params = await asyncio.to_thread(store.run_params, run_id)
    adapter = registry.get_adapter(source)
    if adapter is None:
        raise SourceUnavailable(f"No adapter for {source}.")
    read = 0

    async def store_batch(rows: List[Dict[str, Any]]) -> None:
        nonlocal read
        await asyncio.to_thread(store.insert_alerts, run_id, rows)
        read += len(rows)
        await asyncio.to_thread(store.set_progress, run_id, alerts=read)

    result = await collect.collect_history(
        adapter,
        start=now - timedelta(days=params.history_days + params.review_days),
        store_batch=store_batch,
    )
    await asyncio.to_thread(
        store.record_drops, run_id, "alerts", source, result.dropped
    )
    await asyncio.to_thread(
        store.set_progress, run_id, alerts=result.read, capped=result.capped
    )


async def _rebuild_keeping_the_run(run_id: str) -> None:
    try:
        await asyncio.to_thread(rebuild, run_id, datetime.now(timezone.utc))
    except Exception:
        logger.exception("Cold start run %s kept its build", run_id)


async def _execute(run_id: str, lease: store.WorkerLease) -> None:
    try:
        params = await asyncio.to_thread(store.run_params, run_id)
        now = datetime.now(timezone.utc)
        if params.status is RunStatus.COLLECTING:
            if params.source != collect.UPLOAD_SOURCE:
                await _collect_from_adapter(run_id, params.source, now)
            await asyncio.to_thread(store.set_progress, run_id, phase="assessing")
            await asyncio.to_thread(rebuild, run_id, datetime.now(timezone.utc))
            await asyncio.to_thread(store.transition, run_id, RunStatus.JUDGING)
        else:
            # A file uploaded since the last build is in no assessment yet.
            await _rebuild_keeping_the_run(run_id)
        await _judge(run_id)
        # A review saved while judging could not rebuild, because this worker held the
        # lease. Failing here must not lose a good run: go-live rebuilds again and refuses
        # if it still cannot.
        if await asyncio.to_thread(store.load_reviews, run_id):
            await _rebuild_keeping_the_run(run_id)
    except asyncio.CancelledError:
        # The API process is stopping. The run stays where it is, resumable.
        raise
    except Exception as e:
        logger.exception("Cold start run %s failed", run_id)
        await asyncio.to_thread(
            store.transition,
            run_id,
            RunStatus.FAILED,
            error=f"{type(e).__name__}: {e}"[:500],
        )
    finally:
        lease.release()


def _prepare_start(run_id: str) -> None:
    """What can refuse a start, here and not in the task, so the caller can answer it.
    Inside the task, losing the one-active-run index would fail a good ready run."""
    params = store.run_params(run_id)
    if (
        params.source == collect.UPLOAD_SOURCE
        and params.status is RunStatus.COLLECTING
        and not store.has_alerts(run_id)
    ):
        raise NothingToBuild("Upload an alert file first.")
    if params.status is RunStatus.READY:
        store.transition(run_id, RunStatus.JUDGING)


async def start(run_id: str) -> None:
    """Work a run to ``ready`` as a task in this process. Raises RunBusy while any
    process holds the worker lease."""
    lease = await asyncio.to_thread(store.WorkerLease.acquire)
    if lease is None:
        raise store.RunBusy("A cold start run is already being worked on.")
    try:
        await asyncio.to_thread(_prepare_start, run_id)
    except BaseException:
        await asyncio.to_thread(lease.release)
        raise
    task = asyncio.create_task(_execute(run_id, lease))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def rebuild_while_idle(run_id: str, wait_seconds: float = 0) -> None:
    deadline = time.monotonic() + wait_seconds
    lease = store.WorkerLease.acquire()
    while lease is None and time.monotonic() < deadline:
        time.sleep(LEASE_RETRY_SECONDS)
        lease = store.WorkerLease.acquire()
    if lease is None:
        raise store.RunBusy("A cold start run is already being worked on.")
    try:
        rebuild(run_id, datetime.now(timezone.utc))
    finally:
        lease.release()


def _enable_source(adapter: Any, actor: str) -> None:
    """Enabled from now, as the adapters' own first run would be. A source already
    polling keeps its cursor: moving it would skip what it has not read yet. Without
    the global switch the row would sit enabled and nothing would ever poll."""
    name = adapter.name
    row = federation_store.read_source(name) or federation_store.upsert_source(
        name, {"interval_seconds": adapter.default_interval()}
    )
    if row is None:
        raise SourceUnavailable(f"Could not record {name} as a federation source.")
    if not row.get("enabled"):
        updated = federation_store.update_source(
            name, {"enabled": True, "cursor": cursor_at(utcnow())}
        )
        if updated is None:
            raise SourceUnavailable(f"Could not enable {name}.")
    if not federation_store.read_global_settings().get("enabled"):
        federation_store.set_global_settings({"enabled": True}, updated_by=actor)


def go_live(run_id: str, actor: str) -> None:
    """Idempotent: a live run touches nothing, so a repeat cannot move a cursor. The
    source is enabled first, so a failure leaves the run not live and safe to retry."""
    params = store.run_params(run_id)
    if params.status is RunStatus.LIVE:
        return
    if RunStatus.LIVE not in TRANSITIONS[params.status]:
        raise InvalidTransition(params.status, RunStatus.LIVE)
    # The baseline that goes live must include every review saved so far.
    rebuild_while_idle(run_id)
    if params.source != collect.UPLOAD_SOURCE:
        adapter = registry.get_adapter(params.source)
        if adapter is None or not adapter.is_configured():
            raise SourceUnavailable(f"{params.source} is not configured.")
        _enable_source(adapter, actor)
    store.mark_live(run_id)
