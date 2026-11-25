"""Install Readiness, computed when it is read.

Ready when the background service is running, the assigned model's provider
is routable, at least one alert source is switched on, every switched-on
source is healthy, and every alert source is collected. Any input that cannot
be read makes the whole state unknown. Unknown is not ready, and this blocks
nothing. On the desktop app there is no background service, so the state is
that one line and there is no unmet item.

Not ``/readyz`` or ``/api/health/ready``: those say whether this process is up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_settings
from core.integrations import health
from core.llm.bifrost import path_health
from core.platform.process_health import ProcessHealth
from core.storage.connection import get_db_manager
from core.storage.models import AIModelConfig, LLMProviderConfig

logger = logging.getLogger(__name__)

READY = "ready"
NOT_READY = "not_ready"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

DESKTOP_LINE = "not available on the desktop app: no background service"

# Home's "Connect more tools" step watches these. The daemon and the model do not.
SETUP_AREAS = frozenset({"alert_source", "source"})

_ALERT_KINDS = frozenset({"pulled", "kafka", "receiver"})
_MODEL_HREF = "/settings?section=ai-config&tab=assignment"
_INTEGRATIONS_HREF = "/settings?section=integrations"
_FEDERATION_HREF = "/settings?section=federation"

_WITHHOLDS = frozenset(
    {health.NO_ATTEMPT, health.UNHEALTHY, health.REQUIRES_TEST, health.UNKNOWN}
)


def sources_settled(result: Dict[str, Any]) -> Optional[bool]:
    """Whether "Connect more tools" is done.

    True when no alert-source or switched-on-source item is unmet. False when
    the read is unknown. None on the desktop app, which does not judge sources.
    """
    if result["state"] == UNAVAILABLE:
        return None
    if result["state"] == UNKNOWN:
        return False
    return not any(item["area"] in SETUP_AREAS for item in result["items"])


async def evaluate(processes: ProcessHealth) -> Dict[str, Any]:
    """The install's readiness. ``processes`` is the daemon probe."""
    if get_settings().vigil_desktop_app:
        return {
            "state": UNAVAILABLE,
            "line": DESKTOP_LINE,
            "items": [],
            "notes": [],
        }
    states = await processes.read()
    daemon = states["daemon"]
    if daemon == "unknown":
        return _unknown("Could not read the background service.")
    try:
        sources = await asyncio.to_thread(health.collect)
    except Exception as exc:  # noqa: BLE001 - the state is unknown, not a guess
        logger.error("Readiness could not read health: %s", type(exc).__name__)
        return _unknown("Could not read integration health.")
    try:
        provider = await asyncio.to_thread(_assigned_provider)
    except Exception as exc:  # noqa: BLE001
        logger.error("Readiness could not read assignments: %s", type(exc).__name__)
        return _unknown("Could not read model assignments.")
    try:
        providers = await _providers()
    except Exception as exc:  # noqa: BLE001
        logger.error("Readiness could not read routability: %s", type(exc).__name__)
        return _unknown("Could not read whether the model provider is routable.")
    if not providers:
        # An empty map is what the routability read returns when it could not
        # be made. That is unknown, not "the provider is down".
        return _unknown("Could not read whether the model provider is routable.")
    return _from(daemon, sources, provider, providers)


def _unknown(line: str) -> Dict[str, Any]:
    return {"state": UNKNOWN, "line": line, "items": [], "notes": []}


def _assigned_provider() -> Optional[Tuple[Optional[str]]]:
    """``reporting``, else ``chat_default``, from the assignment rows only.

    None when neither row exists. Otherwise a 1-tuple of the assigned
    provider row's ``provider_type``, or of None when that row is missing or
    inactive. A store error raises. The default active provider is not a
    third step.
    """
    with get_db_manager().session_scope() as session:
        rows = dict(
            session.query(AIModelConfig.component, AIModelConfig.provider_id)
            .filter(AIModelConfig.component.in_(("reporting", "chat_default")))
            .all()
        )
        component = next((c for c in ("reporting", "chat_default") if c in rows), None)
        if component is None:
            return None
        row = (
            session.query(LLMProviderConfig.provider_type)
            .filter(
                LLMProviderConfig.provider_id == rows[component],
                LLMProviderConfig.is_active.is_(True),
            )
            .first()
        )
    return (row[0] if row else None,)


async def _providers() -> Dict[str, bool]:
    snap = await path_health.read()
    try:
        body = path_health.routability_body(snap)
    except path_health.UNREACHABLE:
        return {}
    providers = body.get("providers") or {}
    return {str(name): bool(flag) for name, flag in providers.items() if name}


def _item(
    item_id: str, text: str, fix: str, area: str, href: Optional[str] = None
) -> Dict[str, Any]:
    item = {"id": item_id, "text": text, "fix": fix, "area": area}
    if href:
        item["href"] = href
    return item


def _withholds(state: str, reason: Optional[str]) -> bool:
    if state == health.NOT_CHECKED:
        # The legacy poll keeps no health. "no read-only call exists" does not.
        return reason == health.LEGACY_REASON
    return state in _WITHHOLDS


def _source_fix(state: str) -> str:
    if state == health.NO_ATTEMPT:
        return "It has not succeeded since it was switched on."
    if state == health.REQUIRES_TEST:
        return "Confirm the credential and its read access, then test it."
    if state == health.UNKNOWN:
        return "Its health record has not been updated."
    return "Check the credential and the endpoint."


def _source_text(source_id: str, state: str, reason: Optional[str]) -> str:
    if state == health.NO_ATTEMPT:
        return f"{source_id}: no attempt yet."
    if state == health.REQUIRES_TEST:
        return f"{source_id}: requires a test."
    if state == health.UNKNOWN:
        return f"{source_id}: unknown."
    if reason:
        return f"{source_id}: {reason}."
    return f"{source_id}: {state.replace('_', ' ')}."


def _from(
    daemon: str,
    sources: List[Dict[str, Any]],
    provider: Optional[Tuple[Optional[str]]],
    providers: Dict[str, bool],
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    notes: List[Dict[str, str]] = []
    if daemon == "not_running":
        items.append(
            _item(
                "daemon",
                "The background service is stopped.",
                "Start the background service.",
                "daemon",
            )
        )
    elif daemon != "running":
        items.append(
            _item(
                "daemon",
                "The background service is not serving.",
                "It is up, but its health check is not answering ready.",
                "daemon",
            )
        )
    if provider is None:
        items.append(
            _item(
                "model",
                "No model assigned.",
                "Assign a model.",
                "model",
                _MODEL_HREF,
            )
        )
    # Routability is keyed by Bifrost provider name, which is the row's
    # provider_type for console-created and mirrored rows alike.
    elif not (provider[0] and providers.get(provider[0])):
        items.append(
            _item(
                "model",
                "The assigned model's provider is not routable.",
                "Check that provider's key.",
                "model",
                _MODEL_HREF,
            )
        )
    alerts = [
        row for row in sources if row["switched_on"] and row["kind"] in _ALERT_KINDS
    ]
    if not alerts:
        items.append(
            _item(
                "alert-source",
                "No alert source switched on.",
                "Switch on an alert source.",
                "alert_source",
                _INTEGRATIONS_HREF,
            )
        )
    withheld: List[Dict[str, Any]] = []
    for row in sources:
        if not row["switched_on"] or not _withholds(row["state"], row.get("reason")):
            if (
                row["switched_on"]
                and row["kind"] in _ALERT_KINDS
                and row["state"] == health.HEALTHY
                and not (row.get("record") or {}).get("last_data_at")
            ):
                notes.append(
                    {
                        "id": f"received:{row['source_id']}",
                        "text": f"{row['source_id']}: nothing received yet.",
                    }
                )
            elif row["switched_on"] and row["state"] == health.FAILING_ONCE:
                reason = row.get("reason")
                text = f"{row['source_id']}: failing once."
                if reason:
                    text = f"{row['source_id']}: failing once. {reason}."
                notes.append({"id": f"once:{row['source_id']}", "text": text})
            continue
        if row["state"] == health.NOT_CHECKED:
            items.append(
                _item(
                    f"collected:{row['source_id']}",
                    f"{row['source_id']}: {health.LEGACY_REASON}.",
                    "Switch on scheduled collection.",
                    "alert_source",
                    _FEDERATION_HREF,
                )
            )
            continue
        href = _INTEGRATIONS_HREF if row["kind"] in ("integration", "pulled") else None
        withheld.append(
            _item(
                f"source:{row['source_id']}",
                _source_text(row["source_id"], row["state"], row.get("reason")),
                _source_fix(row["state"]),
                "source",
                href,
            )
        )
    withheld.sort(key=lambda item: item["id"])
    items.extend(withheld)
    state = READY if not items else NOT_READY
    line = "Ready." if state == READY else "Not ready."
    return {"state": state, "line": line, "items": items, "notes": notes}
