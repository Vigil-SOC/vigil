"""Integration Health: one stored record per source, and the one judge of it.

The ``source_health`` row (``core/storage/source_health.py``) is the only
stored copy of a source's health. Its writers are scheduled collection (``core/federation/store.py``), Test
(``services/api/routers/config.py``), a successful MCP read for an integration that
scheduled collection does not poll (``core/agents/mcp_tools.py``),
detection-source syncs (``core/detections/detection_rules_service.py``), the push sources
(``core/storage/push_health.py``: Kafka, the daemon webhook, and the Darktrace
and Cloudflare receivers) and the integration save path (a reset). Every writer
is best-effort: a failed health write is logged and never fails the poll, test,
tool call, push or save it describes. ``judge`` is the only place a state is
computed; a record its writer stopped refreshing reads "unknown since <time>"
there, never healthy. ``labels`` on the row is information only and is not read
here. ``filtered_total`` counts findings the severity floor skipped. It is not
a drop, and it is not read here.

Keys are integration ids, plus prefixed ids for Vigil's own inputs that cannot
collide with one: ``kafka``, ``webhook:<name>`` and
``detection_source:<id from detection_sources.json>``. A declared MCP source
(``core.sources``) is ``source-<name>``, the id its credentials use; the MCP
client writes it at each connect attempt, with the reason it is left out.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.storage.source_health import (  # noqa: F401 - the writers, re-exported
    DROP_WINDOW,
    FEDERATION_INTEGRATION,
    LEGACY_POLLED,
    read,
    read_all,
    record_collected,
    record_failed,
    record_lookup,
    record_polled,
    record_test,
    reset,
    seed,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

KAFKA = "kafka"
RECEIVERS = ("webhook:daemon", "webhook:darktrace", "webhook:cloudflare")
DETECTION_PREFIX = "detection_source:"

UNHEALTHY_FAILURES = 3  # Medic's polls-failing threshold

HEALTHY = "healthy"
NO_ATTEMPT = "no_attempt"
UNHEALTHY = "unhealthy"
FAILING_ONCE = "failing_once"
REQUIRES_TEST = "requires_test"
NOT_CHECKED = "not_checked"
SWITCHED_OFF = "switched_off"
UNKNOWN = "unknown"

LEGACY_REASON = "collected by the older polling path, which keeps no health"


def health_id(federation_source_id: str) -> str:
    """The record key for a federation source: its integration id."""
    return FEDERATION_INTEGRATION.get(federation_source_id, federation_source_id)


def last_test(
    record: Optional[Dict[str, Any]], has_check: bool
) -> Optional[Dict[str, Any]]:
    """``{at, success, error}`` of the last Test, as the integrations API served it.

    Test stamps the attempt and the check with one time, so when they match the
    attempt holds Test's whole answer; after a later poll the check is what is
    left of it. Without a real check only Test writes the attempt.
    """
    if not record:
        return None
    at, check_at = record.get("last_attempt_at"), record.get("last_check_at")
    if check_at and check_at != at:
        return {
            "at": check_at,
            "success": record.get("last_check_ok"),
            "error": record.get("last_check_error"),
        }
    if at and (check_at or not has_check):
        return {
            "at": at,
            "success": record.get("last_attempt_ok"),
            "error": record.get("last_error"),
        }
    return None


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def _state(state: str, reason: Optional[str] = None, since=None) -> Dict[str, Any]:
    return {"state": state, "reason": reason, "since": since}


def judge(
    record: Optional[Dict[str, Any]],
    *,
    kind: str,
    switched_on: bool,
    has_check: bool = True,
    unchecked: Optional[str] = None,
    legacy: bool = False,
    interval_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The one state of a source, from its record and how it is set up.

    ``kind`` is ``integration``, ``pulled``, ``kafka``, ``receiver``,
    ``detection_source`` or ``mcp_source``. ``interval_seconds`` is set only while scheduled
    collection polls the source; a record older than that interval allows
    (backoff included) is no longer believed and reads "unknown".
    ``unchecked`` is the reason an integration is not checked (no read-only
    call, or a fixed call with no credential saved). That state does not
    withhold readiness, and nothing is stored for it.
    """
    from core.federation.lag import overdue

    now = now or utcnow()
    rec = record or {}
    if not switched_on:
        return _state(SWITCHED_OFF)
    if legacy:
        return _state(NOT_CHECKED, LEGACY_REASON)
    if kind == "integration" and unchecked:
        return _state(NOT_CHECKED, unchecked)
    attempt = rec.get("last_attempt_at")
    failures = int(rec.get("consecutive_failures") or 0)
    check_ok = bool(rec.get("last_check_at") and rec.get("last_check_ok"))

    if kind == "receiver" and not rec.get("last_success_at"):
        return _state(REQUIRES_TEST, "nothing accepted yet")
    if kind == "integration" and not has_check and not check_ok:
        return _state(REQUIRES_TEST, "Vigil has no real check for this integration")
    if interval_seconds and attempt:
        # Not refreshed for longer than polling allows: whatever it last said,
        # it no longer says how the source is. The runner backs off up to 8x on
        # failures; one more interval of slack.
        allowed = interval_seconds * min(2**failures, 8)
        if overdue(attempt, allowed, now, slack=max(interval_seconds, 60)):
            return _state(UNKNOWN, "health not updated", rec.get("updated_at"))
    if failures >= UNHEALTHY_FAILURES:
        return _state(UNHEALTHY, f"{failures} failures in a row", attempt)
    dropped_at = rec.get("last_dropped_at")
    if dropped_at and now - dropped_at < DROP_WINDOW:
        return _state(
            UNHEALTHY, "dropped or unreadable records in the last hour", dropped_at
        )
    if not attempt:
        return _state(NO_ATTEMPT)
    if rec.get("last_attempt_ok") is False:
        return _state(
            FAILING_ONCE, rec.get("last_error") or "last attempt failed", attempt
        )
    confirmed = max(
        (
            t
            for t in (rec.get("last_success_at"), check_ok and rec["last_check_at"])
            if t
        ),
        default=attempt,
    )
    return _state(HEALTHY, "last confirmed working", confirmed)


# ---------------------------------------------------------------------------
# All sources, judged (GET /api/integration-health)
# ---------------------------------------------------------------------------


def _push_inputs() -> List[Tuple[str, str, bool]]:
    """(source id, kind, switched on) for Kafka and the three receivers."""
    from core.config import get_settings
    from core.ingestion.kafka_config import KafkaConfig, apply_kafka_settings
    from core.integrations.cloudflare.cloudflare_webhooks_router import (
        cloudy_ingestion_enabled,
    )
    from core.integrations.darktrace.darktrace_webhook_router import darktrace_enabled
    from core.secrets import get_secret
    from core.storage.config_service import get_config_service

    kafka = KafkaConfig.from_env()
    apply_kafka_settings(
        kafka, get_config_service().read_system_config("kafka.settings") or {}
    )
    return [
        (KAFKA, "kafka", bool(kafka.enabled)),
        (
            "webhook:daemon",
            "receiver",
            bool(
                get_settings().daemon_webhook_enabled
                and get_secret("DAEMON_WEBHOOK_TOKEN")
            ),
        ),
        (
            "webhook:darktrace",
            "receiver",
            bool(darktrace_enabled() and get_secret("DARKTRACE_WEBHOOK_SECRET")),
        ),
        (
            "webhook:cloudflare",
            "receiver",
            bool(cloudy_ingestion_enabled() and get_secret("CLOUDY_WEBHOOK_SECRET")),
        ),
    ]


def _server_switches() -> Optional[Dict[str, bool]]:
    """The saved MCP server switches, read once per collect; None when the file
    can't be read, and then no integration is taken as switched off."""
    from core.integrations.mcp.service import MCPService

    try:
        switches = MCPService.enabled_state()
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("MCP server switches unreadable, health ignores them: %s", exc)
        return None
    if not isinstance(switches, dict):
        logger.warning("MCP server switches are not a mapping; health ignores them")
        return None
    return switches


def _servers_switched_off(
    integration_id: str, switches: Optional[Dict[str, bool]]
) -> bool:
    """True when the integration has MCP servers and every one is switched off.

    For an integration that is not an extension, the Connected row's only switch
    is its server's: off, agents hold none of its tools, so its last check says
    nothing about an integration in use. One with no server, or no descriptor,
    has no such switch."""
    from core.integrations._base.descriptor import get_descriptor
    from core.integrations.mcp.service import MCPService

    descriptor = get_descriptor(integration_id)
    servers = descriptor.mcp_server_names if descriptor else ()
    if switches is None or not servers:
        return False
    return not any(MCPService.is_server_enabled(s, switches) for s in servers)


def _integration_rows() -> List[Dict[str, Any]]:
    """``{integration_id, enabled}`` per saved integration; a store error raises
    (the config service's list reads an error as no integrations)."""
    from core.storage.connection import get_db_manager
    from core.storage.models import IntegrationConfig

    with get_db_manager().session_scope() as session:
        return [
            {"integration_id": i, "enabled": e}
            for i, e in session.query(
                IntegrationConfig.integration_id, IntegrationConfig.enabled
            )
        ]


_POLL_COUNTS = ("returned_window", "stored_window", "filtered_window")


def with_window(
    record: Optional[Dict[str, Any]], now: datetime
) -> Optional[Dict[str, Any]]:
    """``record`` plus the derived ``duplicate_window``: records the runner's ID
    dedup skipped. A negative remainder is an accounting error and shows as one.

    A window older than ``DROP_WINDOW`` is not current, so its counts read null.
    So do poll counts no successful federation poll has initialized (push
    sources keep their drop window). ``dropped_total`` is untouched.
    """
    if record is None:
        return None
    start = record.get("dropped_window_start")
    expired = start is None or now - start >= DROP_WINDOW
    counted = not expired and all(record.get(f) is not None for f in _POLL_COUNTS)
    out = {**record, "duplicate_window": None}
    if expired:
        out["dropped_window"] = None
    if not counted:
        out.update({f: None for f in _POLL_COUNTS})
        return out
    out["duplicate_window"] = (
        record["returned_window"]
        - record["stored_window"]
        - record["filtered_window"]
        - record["dropped_window"]
    )
    return out


def collect(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every source with its record and state. A store read error raises."""
    from core.audit.feed import INTEGRATION_ID, WEBHOOK_INTEGRATION_ID
    from core.detections.detection_rules_service import read_saved_sources
    from core.federation import store as fed_store
    from core.integrations.credential_check import has_check, not_checked_reason
    from core.sources import INTEGRATION_PREFIX as SOURCE_PREFIX
    from core.sources import applied_sources, catalogue_names

    now = now or utcnow()
    records = read_all()
    fed_on = bool(fed_store.read_global_settings().get("enabled"))
    fed_rows = {r["source_id"]: r for r in fed_store.read_sources(with_health=False)}
    integrations = _integration_rows()
    switches = _server_switches()
    out: List[Dict[str, Any]] = []

    def add(source_id: str, kind: str, switched_on: bool, **judge_kw) -> None:
        record = records.get(source_id)
        out.append(
            {
                "source_id": source_id,
                "kind": kind,
                "switched_on": switched_on,
                **judge(
                    record, kind=kind, switched_on=switched_on, now=now, **judge_kw
                ),
                "record": with_window(record, now),
            }
        )

    for row in sorted(integrations, key=lambda r: r["integration_id"]):
        iid = row["integration_id"]
        if iid in (INTEGRATION_ID, WEBHOOK_INTEGRATION_ID):
            continue  # the audit feed has its own health (GET /api/audit/feed)
        if iid.startswith(SOURCE_PREFIX):
            continue  # a declared source's credentials; its row is below
        fed_id = LEGACY_POLLED.get(iid)
        fed_row = fed_rows.get(fed_id) if fed_id else None
        scheduled = bool(fed_on and fed_row and fed_row.get("enabled"))
        # A pulled source is judged on collection, which runs without its server.
        on = bool(row.get("enabled")) and (
            bool(fed_id) or not _servers_switched_off(iid, switches)
        )
        add(
            iid,
            "pulled" if fed_id else "integration",
            on,
            has_check=has_check(iid),
            unchecked=not_checked_reason(iid),
            legacy=bool(fed_id) and not scheduled,
            interval_seconds=(
                int(fed_row["interval_seconds"]) if fed_row and scheduled else None
            ),
        )
    for source_id, kind, on in _push_inputs():
        add(source_id, kind, on)
    for source in read_saved_sources():
        add(f"{DETECTION_PREFIX}{source['id']}", "detection_source", True)
    # One row per applied declared source. Accepted, it starts switched on.
    catalogue = catalogue_names()
    for name in sorted(applied_sources().get("sources") or {}):
        # A catalogue server of this name wins; its switch is not the source's.
        on = switches is None or name in catalogue or bool(switches.get(name, True))
        add(f"{SOURCE_PREFIX}{name}", "mcp_source", on)
    return out
