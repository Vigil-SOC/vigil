"""What changed since the Day 1 Report a production run is read against.

The list is stored on the record. The new and fixed labels are not: those
are applied when the record is read. Nothing here names an account.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from core.audit import trail
from core.auth.permissions import missing_permission
from core.federation.store import read_sources
from core.findings.exclusions import list_exclusions_with_total
from core.integrations import health
from core.reporting.day1 import (
    NOT_CHECKED,
    _iso,
    _parse,
    day1_anchor,
)
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

NO_DAY1 = "not checked: no Day 1 Report to compare with"
_AUDIT_UNREADY = "not checked: the audit trail is not ready"
_AUDIT_UNREAD = "not checked: could not read the audit trail."
_EXCLUSIONS_UNREAD = "not checked: could not read exclusions."
_HEALTH_UNREAD = "not checked: could not read integration health."
_CHANGES_UNREAD = "not checked: could not read what changed."
_CONFIG_CHANGE = "config.change"
_CEILING = "autonomy.ceiling"
_FEDERATION = "federation.settings"
_SOURCE_PREFIX = "federation.source:"
_ALERT_KINDS = frozenset({"pulled", "kafka", "receiver"})
_TIERS = frozenset({"act", "assist"})
_AUDIT_KINDS = frozenset({"autonomy", "collection", "source"})
_AUTONOMY_SCREEN = "/settings?section=autoinvestigate"
_COLLECTION_SCREEN = "/settings?section=federation"
_INTEGRATIONS_SCREEN = "/settings?section=integrations"
_EXCLUSIONS_SCREEN = "/dashboard"
_AUDIT_WITHHELD = (
    "Not included: autonomy and scheduled-collection changes. This needs audit.read."
)
_FINDINGS_WITHHELD = "Not included: exclusions. This needs findings.read."
_INTEGRATIONS_WITHHELD = (
    "Not included: alert sources switched off since the Day 1 Report. "
    "This needs integrations.read."
)


def produce(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """Changes from the Day 1 anchor through ``window_end``.

    With no Day 1 record, the section is the not-checked sentence and not a
    list of everything. An empty list is still returned when nothing changed.
    """
    end = _parse(window_end)
    if end is None:
        return {"not_checked": _CHANGES_UNREAD}
    floors, floors_unread = _floors()
    try:
        with unit_of_work() as session:
            anchor, mode = day1_anchor(session, _parse(started_at))
            if anchor is None:
                return {"not_checked": NO_DAY1}
            start = anchor.recorded_at
            checks = [dict(check) for check in (anchor.checks or [])]
            anchor_at = _iso(start)
            body = _from_stores(session, start, end, floors, floors_unread)
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error("Production changes could not be read: %s", type(exc).__name__)
        return {"not_checked": _CHANGES_UNREAD}
    body["anchor_at"] = anchor_at
    body["before_not_shown"] = mode == "before"
    switched, health_note, anchor_unread = _switched_off(checks)
    if health_note:
        body["health_not_checked"] = health_note
    elif anchor_unread:
        body["health_anchor_unread"] = True
    else:
        body["items"].extend(switched)
    body["items"].sort(key=lambda item: item.get("at") or "~")
    return body


def present(user_id: str, stored: dict[str, Any]) -> dict[str, Any]:
    """Drop items the caller cannot read, and name each missing gate.

    A missing gate leaves the other items in place. The stored record is not
    changed.
    """
    if stored.get("not_checked") and not stored.get("anchor_at"):
        return {"not_checked": stored["not_checked"]}
    audit = missing_permission(user_id, "audit.read") is None
    findings = missing_permission(user_id, "findings.read") is None
    integrations = missing_permission(user_id, "integrations.read") is None
    shown: dict[str, Any] = {
        "anchor_at": stored.get("anchor_at"),
        "before_not_shown": bool(stored.get("before_not_shown")),
        "items": [],
        "withheld": [],
    }
    if not audit:
        shown["withheld"].append(_AUDIT_WITHHELD)
    elif isinstance(stored.get("audit_not_checked"), str):
        shown["audit_not_checked"] = stored["audit_not_checked"]
    if not findings:
        shown["withheld"].append(_FINDINGS_WITHHELD)
    elif isinstance(stored.get("exclusions_not_checked"), str):
        shown["exclusions_not_checked"] = stored["exclusions_not_checked"]
    if not integrations:
        shown["withheld"].append(_INTEGRATIONS_WITHHELD)
    elif isinstance(stored.get("health_not_checked"), str):
        shown["health_not_checked"] = stored["health_not_checked"]
    elif stored.get("health_anchor_unread"):
        shown["health_anchor_unread"] = True
    for item in stored.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind in _AUDIT_KINDS and not audit:
            continue
        if kind == "exclusion" and not findings:
            continue
        if kind == "switched_off" and not integrations:
            continue
        shown["items"].append(dict(item))
    return shown


def _from_stores(
    session,
    start: datetime,
    end: datetime,
    floors: dict[str, Any],
    floors_unread: bool,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    body: dict[str, Any] = {"items": items}
    audit_items, audit_note = _audit(session, start, end, floors, floors_unread)
    if audit_note:
        body["audit_not_checked"] = audit_note
    else:
        items.extend(audit_items)
    exclusion_items, exclusion_note = _exclusions(session, start, end)
    if exclusion_note:
        body["exclusions_not_checked"] = exclusion_note
    else:
        items.extend(exclusion_items)
    return body


def _floors() -> tuple[dict[str, Any], bool]:
    try:
        rows = read_sources(with_health=False)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Production changes could not read severity floors: %s",
            type(exc).__name__,
        )
        return {}, True
    floors: dict[str, Any] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("source_id"), str):
            floors[row["source_id"]] = row.get("min_severity")
    return floors, False


def _audit(
    session,
    start: datetime,
    end: datetime,
    floors: dict[str, Any],
    floors_unread: bool,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    if not trail.in_use(session):
        return [], _AUDIT_UNREADY
    try:
        rows = trail.read_all(
            session, trail.make_query(since=start, until=end, area="config")
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Production changes could not read the audit trail: %s",
            type(exc).__name__,
        )
        return [], _AUDIT_UNREAD
    items = []
    for entry in rows:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict) or payload.get("action") != _CONFIG_CHANGE:
            continue
        key = payload.get("config_key")
        if not isinstance(key, str):
            continue
        at = _iso(_parse(entry.get("occurred_at")))
        old = payload.get("old_value")
        new = payload.get("new_value")
        if key == _CEILING:
            item = _autonomy(old, new, at)
        elif key == _FEDERATION:
            item = _collection(old, new, at)
        elif key.startswith(_SOURCE_PREFIX):
            item = _source(
                key[len(_SOURCE_PREFIX) :], old, new, at, floors, floors_unread
            )
        else:
            item = None
        if item is not None:
            items.append(item)
    return items, None


def _flag(value: Any, name: str) -> bool:
    return isinstance(value, dict) and value.get(name) is True


def _tiers(value: Any) -> dict[str, str]:
    workflows = value.get("workflows") if isinstance(value, dict) else None
    if not isinstance(workflows, dict):
        return {}
    found = {}
    for workflow_id, entry in workflows.items():
        if isinstance(entry, dict) and entry.get("tier") in _TIERS:
            found[str(workflow_id)] = entry["tier"]
    return found


def _autonomy(old: Any, new: Any, at: Optional[str]) -> Optional[dict[str, Any]]:
    flags = []
    for name in ("reversible", "irreversible"):
        before = _flag(old, f"act_{name}")
        after = _flag(new, f"act_{name}")
        if before != after:
            flags.append({"name": name, "old": before, "new": after})
    before_tiers = _tiers(old)
    after_tiers = _tiers(new)
    tiers = []
    for workflow_id in sorted(set(before_tiers) | set(after_tiers)):
        if before_tiers.get(workflow_id) != after_tiers.get(workflow_id):
            tiers.append(
                {
                    "workflow": workflow_id,
                    "old": before_tiers.get(workflow_id),
                    "new": after_tiers.get(workflow_id),
                }
            )
    if not flags and not tiers:
        return None
    return {
        "kind": "autonomy",
        "at": at,
        "screen": _AUTONOMY_SCREEN,
        "flags": flags,
        "tiers": tiers,
    }


def _enabled(value: Any) -> Optional[bool]:
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return value["enabled"]
    return None


def _collection(old: Any, new: Any, at: Optional[str]) -> Optional[dict[str, Any]]:
    before = _enabled(old)
    after = _enabled(new)
    if before == after:
        return None
    return {
        "kind": "collection",
        "at": at,
        "screen": _COLLECTION_SCREEN,
        "old": before,
        "new": after,
    }


def _plain(value: Any, key: str) -> Any:
    if not isinstance(value, dict) or key not in value:
        return None
    item = value.get(key)
    if isinstance(item, bool) or isinstance(item, str):
        return item
    if isinstance(item, int):
        return item
    return None


def _pair(old: Any, new: Any, key: str) -> dict[str, Any]:
    return {"old": _plain(old, key), "new": _plain(new, key)}


def _source(
    source_id: str,
    old: Any,
    new: Any,
    at: Optional[str],
    floors: dict[str, Any],
    floors_unread: bool,
) -> Optional[dict[str, Any]]:
    if not source_id:
        return None
    enabled = _pair(old, new, "enabled")
    interval = _pair(old, new, "interval_seconds")
    cap = _pair(old, new, "max_items")
    floor = _pair(old, new, "min_severity")
    unchanged = (
        enabled["old"] == enabled["new"]
        and interval["old"] == interval["new"]
        and cap["old"] == cap["new"]
        and floor["old"] == floor["new"]
    )
    if unchanged:
        return None
    item: dict[str, Any] = {
        "kind": "source",
        "at": at,
        "screen": _COLLECTION_SCREEN,
        "source_id": source_id,
        "enabled": enabled,
        "interval_seconds": interval,
        "cap": cap,
        "min_severity": floor,
    }
    if floors_unread:
        item["current_floor_unread"] = True
    else:
        current = floors.get(source_id)
        item["current_min_severity"] = current if isinstance(current, str) else None
    return item


def _in_window(value: Any, start: datetime, end: datetime) -> Optional[datetime]:
    parsed = _parse(value)
    if parsed is None or parsed < start or parsed > end:
        return None
    return parsed


def _exclusions(
    session, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        rows, _total = list_exclusions_with_total(session, include_removed=True)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Production changes could not read exclusions: %s", type(exc).__name__
        )
        return [], _EXCLUSIONS_UNREAD
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = row.get("ip")
        if not isinstance(ip, str) or not ip:
            continue
        created = _in_window(row.get("created_at"), start, end)
        if created is not None:
            item: dict[str, Any] = {
                "kind": "exclusion",
                "action": "added",
                "at": _iso(created),
                "screen": _EXCLUSIONS_SCREEN,
                "ip": ip,
            }
            hidden = row.get("hidden_findings")
            if row.get("active") is True and isinstance(hidden, int):
                item["hidden_findings"] = hidden
            items.append(item)
        removed = _in_window(row.get("removed_at"), start, end)
        if removed is not None:
            items.append(
                {
                    "kind": "exclusion",
                    "action": "removed",
                    "at": _iso(removed),
                    "screen": _EXCLUSIONS_SCREEN,
                    "ip": ip,
                }
            )
    return items, None


def _switched_off(
    checks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Optional[str], bool]:
    """Sources an anchor visibility check named that are off now.

    The anchor's not-checked ``visibility.source`` check is the unread flag.
    An empty switched-off list is not that flag.
    """
    for check in checks:
        if (
            check.get("id") == "visibility.source"
            and check.get("outcome") == NOT_CHECKED
        ):
            return [], None, True
    try:
        sources = health.collect()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Production changes could not read integration health: %s",
            type(exc).__name__,
        )
        return [], _HEALTH_UNREAD, False
    by_id = {
        row.get("source_id"): row
        for row in sources
        if isinstance(row, dict) and row.get("source_id")
    }
    items = []
    seen: set[str] = set()
    prefix = "visibility.source."
    for check in checks:
        check_id = str(check.get("id") or "")
        if not check_id.startswith(prefix):
            continue
        source_id = check_id[len(prefix) :]
        if not source_id or source_id in seen:
            continue
        row = by_id.get(source_id)
        if not isinstance(row, dict) or row.get("switched_on"):
            continue
        if row.get("kind") not in _ALERT_KINDS:
            continue
        seen.add(source_id)
        items.append(
            {
                "kind": "switched_off",
                "screen": _INTEGRATIONS_SCREEN,
                "source_id": source_id,
            }
        )
    return items, None, False
