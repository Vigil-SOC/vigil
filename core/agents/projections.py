# What a run decided, read from the layer that owns the events. Python is
# permitted two reads against agent_events -- a count and the terminal payload --
# so anything richer is asked for rather than folded here.

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from core.config import get_settings
from core.secrets import get_secret

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


# None means "nothing to report yet" and is not an error: a run enqueued a moment
# ago has no ledger, and a supervisor must read that as still starting.
async def read_projection(run_id: str) -> Optional[Dict[str, Any]]:
    import httpx

    url = agent_route(f"/runs/{run_id}/projection")
    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as client:
            response = await client.get(url, headers=_headers())
    except Exception as exc:  # noqa: BLE001 — unreachable is not terminal
        logger.debug("could not read the projection for %s: %s", run_id, exc)
        return None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning("projection for %s answered %s", run_id, response.status_code)
        return None
    return response.json()


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
