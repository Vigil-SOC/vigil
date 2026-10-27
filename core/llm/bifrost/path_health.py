"""One cached read of Bifrost routability and the virtual-key quota.

The status line, ``GET /api/bifrost/routability`` and
``GET /api/analytics/budget/quota`` share it. At most one refresh is in
flight, and every caller inside ``CACHE_S`` reuses that snapshot. The quota
read is synchronous httpx, so it runs off the event loop. No lifespan poller:
the first caller after the window starts the refresh.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any, Dict, Optional, Tuple, TypedDict

import httpx

from core.llm.bifrost import costs, mirror
from core.llm.cost import budget as vk_budget

logger = logging.getLogger(__name__)

# The console polls on this same interval from every open shell.
CACHE_S = 30.0

# What the admin routability route already turns into 502: the gateway did
# not answer, including a 200 whose body is not JSON.
UNREACHABLE = (httpx.HTTPError, ValueError)


class Snapshot(TypedDict):
    routability: Optional[Dict[str, Any]]
    routability_error: Optional[BaseException]
    vk: Optional[str]
    quota: Optional[Dict[str, Any]]
    quota_error: Optional[BaseException]


_answer: Optional[Tuple[float, Snapshot]] = None
_reading: Optional[asyncio.Task[Snapshot]] = None


def reset() -> None:
    """Drop the snapshot. Tests call this so one case does not answer the next."""
    global _answer, _reading
    task = _reading
    _answer = None
    _reading = None
    if task is not None and not task.done():
        task.cancel()


def _copy(snap: Snapshot) -> Snapshot:
    return Snapshot(
        routability=copy.deepcopy(snap["routability"]),
        routability_error=snap["routability_error"],
        vk=snap["vk"],
        quota=copy.deepcopy(snap["quota"]),
        quota_error=snap["quota_error"],
    )


async def read() -> Snapshot:
    """The latest snapshot, at most ``CACHE_S`` old."""
    global _reading
    if _answer is not None and time.monotonic() - _answer[0] < CACHE_S:
        return _copy(_answer[1])
    # No await between the check and the assignment, so two callers on this
    # loop cannot both start a refresh.
    if _reading is None or _reading.done():
        _reading = asyncio.ensure_future(_refresh())
    # Shielded: one caller going away must not cancel the refresh the others
    # are waiting on. _refresh does not raise.
    return _copy(await asyncio.shield(_reading))


def routability_body(snap: Snapshot) -> Dict[str, Any]:
    """The routability document. Raises the error the refresh stored."""
    exc = snap["routability_error"]
    if exc is not None:
        raise exc
    body = snap["routability"]
    if not body:
        return {"providers": {}, "keys": {}}
    return body


async def _refresh() -> Snapshot:
    routability, quota = await asyncio.gather(_routability(), _quota())
    snap = Snapshot(
        routability=routability[0],
        routability_error=routability[1],
        vk=quota[0],
        quota=quota[1],
        quota_error=quota[2],
    )
    global _answer
    _answer = (time.monotonic(), snap)
    return snap


async def _routability() -> Tuple[Optional[Dict[str, Any]], Optional[BaseException]]:
    try:
        return await mirror.routability(), None
    except Exception as exc:  # noqa: BLE001 — cached and re-raised by the route
        if isinstance(exc, UNREACHABLE):
            logger.warning("Routability check failed: %s", exc)
        return None, exc


async def _quota() -> (
    Tuple[Optional[str], Optional[Dict[str, Any]], Optional[BaseException]]
):
    try:
        vk = vk_budget.read_vk()
    except Exception as exc:  # noqa: BLE001 — the quota route still answers 500
        return None, None, exc
    if not vk:
        return None, None, None
    return vk, await asyncio.to_thread(costs.get_vk_quota, vk), None
