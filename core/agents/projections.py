# What a run decided, read from the layer that owns the events. Python is
# permitted two reads against agent_events -- a count and the terminal payload --
# so anything richer is asked for rather than folded here.

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.config import get_settings
from core.secrets import get_secret
from core.workflows.workflow_run_service import LIST_RUNS_MAX, WorkflowRunService

logger = logging.getLogger(__name__)

READ_TIMEOUT_S = 10

# An investigation id is ours and is not a uuid; a run id is. Derived rather than
# stored so the same investigation always addresses the same run.
RUNS = uuid.UUID("6ba7b813-9dad-11d1-80b4-00c04fd430c8")


def run_id_for(investigation_id: str) -> str:
    return str(uuid.uuid5(RUNS, investigation_id))


def agent_route(path: str) -> str:
    return f"{get_settings().agent_url.rstrip('/')}{path}"


def _headers() -> Dict[str, str]:
    token = get_secret("AGENT_INTERNAL_TOKEN") or ""
    if not token:
        raise RuntimeError("AGENT_INTERNAL_TOKEN is not configured")
    return {"Authorization": f"Bearer {token}"}


async def _read_fold(run_id: str, view: str) -> Optional[Dict[str, Any]]:
    import httpx

    url = agent_route(f"/runs/{run_id}/{view}")
    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as client:
            response = await client.get(url, headers=_headers())
    except Exception as exc:  # noqa: BLE001 — unreachable is not terminal
        logger.debug("could not read the %s for %s: %s", view, run_id, exc)
        return None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning("%s for %s answered %s", view, run_id, response.status_code)
        return None
    return response.json()


# None means "nothing to report yet" and is not an error: a run enqueued a moment
# ago has no ledger, and a supervisor must read that as still starting.
async def read_projection(run_id: str) -> Optional[Dict[str, Any]]:
    return await _read_fold(run_id, "projection")


# What episodic memory reads once a run has ended, folded on the side that owns
# the events; see services/agent/workflows/hunt/distil.ts for why it is not the
# projection. None reads as "nothing to distil yet", never as "this run saw
# nothing" — the difference matters, because the second would be a fact.
async def read_distil(run_id: str) -> Optional[Dict[str, Any]]:
    return await _read_fold(run_id, "distil")


# Longer than a read: this asks a model to write a whole run's account.
WRITE_TIMEOUT_S = 180


async def write_narrative(run_id: str) -> Dict[str, Any]:
    """Ask the agent layer for a fresh account of ``run_id``.

    Raises rather than answering None: an operator pressed a button and is
    owed the reason it did not work.
    """
    import httpx

    url = agent_route(f"/runs/{run_id}/narrate")
    try:
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_S) as client:
            response = await client.post(url, headers=_headers())
    except httpx.TimeoutException:
        # Its own str() is empty, so re-raised with the one fact that matters.
        raise RuntimeError(
            f"the agent layer was still writing after {WRITE_TIMEOUT_S}s; "
            "the account is written to the ledger when it finishes, "
            "so reopen the run to read it"
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not reach the agent layer: {exc!r}") from None

    if response.status_code != 200:
        detail = response.text[:400]
        raise RuntimeError(f"the agent layer answered {response.status_code}: {detail}")
    return response.json()


async def read_replay(
    run_id: str, decision_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Rebuild what each decision of hunt ``run_id`` was shown, from its ledger.

    None is serve's 404: nothing hunt-like to replay, or an unknown decision.
    Anything else that is not a report raises, because an operator clicked
    Replay and is owed the reason, unlike the polled projection reads.
    """
    import httpx

    url = agent_route(f"/runs/{run_id}/replay")
    params = {"decision_id": decision_id} if decision_id else None
    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as client:
            response = await client.get(url, headers=_headers(), params=params)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not reach the agent layer: {exc!r}") from None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        detail = response.text[:400]
        raise RuntimeError(f"the agent layer answered {response.status_code}: {detail}")
    return response.json()


THREAT_HUNT_WORKFLOW_ID = "threat-hunt"
PROJECTION_READ_CONCURRENCY = 8


def _is_date_only(value: str) -> bool:
    trimmed = value.rstrip("Zz")
    return "T" not in trimmed and " " not in trimmed


def parse_window_instant(value: str, *, end: bool = False) -> datetime:
    """ISO-8601 to naive UTC, matching workflow_runs columns.

    A date-only ``end`` is the last microsecond of that UTC day so an
    assessment window named by dates includes the end date.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if end and _is_date_only(value):
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


async def _read_gated(run_id: str, gate: asyncio.Semaphore) -> Optional[Dict[str, Any]]:
    async with gate:
        return await read_projection(run_id)


# Completed threat-hunt projections for an assessment window. The projection is
# the pack: hypotheses (with provenance), evidence provenance, verdict, checkpoint
# resolutions, timestamps. A missing projection is skipped, not folded from
# agent_events. If every listed run fails to read, that is an error, not an
# empty pack.
async def pack_completed_hunts(
    *, start: str, end: str, limit: int = LIST_RUNS_MAX
) -> Dict[str, Any]:
    started_at = parse_window_instant(start)
    finished_at = parse_window_instant(end, end=True)
    if started_at > finished_at:
        raise ValueError("start must be at or before end")
    cap = max(1, min(int(limit), LIST_RUNS_MAX))
    runs = WorkflowRunService().list_runs(
        workflow_id=THREAT_HUNT_WORKFLOW_ID,
        status="completed",
        finished_after=started_at,
        finished_at=finished_at,
        limit=cap,
    )
    gate = asyncio.Semaphore(PROJECTION_READ_CONCURRENCY)
    projections = await asyncio.gather(
        *(_read_gated(run["run_id"], gate) for run in runs)
    )
    hunts = [projection for projection in projections if projection is not None]
    if runs and not hunts:
        raise RuntimeError(
            "could not read hunt projections for completed runs in the window"
        )
    return {"start": start, "end": end, "hunts": hunts}
