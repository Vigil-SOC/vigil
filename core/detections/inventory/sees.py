"""Sees: whether the data each active Kibana rule reads is arriving.

Wazuh rules get theirs from ``wazuh_sees``, which writes through ``_write``;
Splunk rules get none yet.

A rule's data source is its ``index`` patterns (or its data view's) and the
timestamp field its 7-day range runs on. Rules on one source share one search:
a size-0 ``_search`` that stops at the first event, the only call counted
against elastic-siem's ``max_searches_per_hour``. ``_field_caps`` then says
whether the rule's ``required_fields`` are mapped. Groups are searched one at a
time, the least recently searched first; when the hour's budget is spent the
rest wait for the next sweep.

The result goes under checks['sees'] on the latest version, written only when
it changed. ES|QL, ML and rules with no index read ``unknown``, never ``pass``.
Nothing is written to Elasticsearch or Kibana.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, bindparam, select, tuple_, update

from core.detections.inventory import kibana
from core.integrations._base import search_budget
from core.integrations._base.search_params import validate_index
from core.integrations.elastic.descriptor import ELASTIC
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

DAYS = 7
DEFAULT_PER_HOUR = 30
INTEGRATION = ELASTIC.id

Key = Tuple[Tuple[str, ...], str]  # (patterns, timestamp field)
RuleKey = Tuple[int, int]  # (rule id, version)

# Group -> monotonic time it was last searched. A restart re-sweeps.
last_searched: Dict[Key, float] = {}


@dataclass
class SeesReport:
    source: str = "elastic:sees"
    rules: int = 0
    groups: int = 0
    searched: int = 0
    deferred: int = 0
    written: int = 0
    error: Optional[str] = None


def _unknown(reason: str, data_source: Optional[str] = None) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason, "data_source": data_source}


def _fields(content: Mapping[str, Any]) -> List[str]:
    return sorted(
        {
            f["name"]
            for f in content.get("required_fields") or []
            if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]
        }
    )


async def _patterns(
    service: Any, content: Mapping[str, Any], views: Dict[str, Any]
) -> Tuple[List[str], Optional[str]]:
    """The rule's index patterns, or why there are none. A data view is read once a sweep."""
    kind = content.get("type")
    if kind == "esql":
        return [], "ES|QL rule: its data source is not parsed"
    if kind == "machine_learning":
        return [], "machine learning rule: reads an ML job, not an index"
    index = [p for p in content.get("index") or [] if isinstance(p, str) and p]
    if index:
        return index, None
    view = content.get("data_view_id")
    if not view:
        return [], "the rule names no index or data view"
    if view not in views:
        try:
            title = (await service.get_data_view(str(view))).get("title") or ""
            views[view] = ([p.strip() for p in title.split(",") if p.strip()], None)
        except Exception as exc:  # noqa: BLE001 -- the rule reads unknown
            views[view] = ([], f"data view {view} could not be read: {exc}")
    patterns, error = views[view]
    return patterns, error or (None if patterns else f"data view {view} has no index")


def _verdict(
    index: str, ts: str, fields: List[str], present: Optional[set], caps_error: str
) -> Dict[str, Any]:
    if present is None:
        return _unknown(
            f"events in {index} in {DAYS}d, but its field caps failed: {caps_error}",
            index,
        )
    missing = [f for f in fields if f not in present]
    if missing:
        return {
            "result": "fail",
            "reason": f"fail: {', '.join(missing)} not in the mapping of {index}",
            "data_source": index,
        }
    reason = (
        None
        if fields
        else "index only: the rule declares no required_fields, so no field was checked"
    )
    return {"result": "pass", "reason": reason, "data_source": index}


async def _check(
    service: Any,
    rules: List[Tuple[RuleKey, Mapping[str, Any]]],
    per_hour: int,
    report: SeesReport,
) -> Dict[RuleKey, Dict[str, Any]]:
    results: Dict[RuleKey, Dict[str, Any]] = {}
    groups: Dict[Key, List[Tuple[RuleKey, List[str]]]] = {}
    views: Dict[str, Any] = {}
    for key, content in rules:
        patterns, why = await _patterns(service, content, views)
        if why:
            results[key] = _unknown(why)
            continue
        # An exclusion applies only to the patterns before it, so keep that order.
        ordered = tuple(
            patterns if any(p.startswith("-") for p in patterns) else sorted(patterns)
        )
        index = ",".join(ordered)
        try:
            validate_index(index)
        except ValueError as exc:
            results[key] = _unknown(str(exc), index)
            continue
        ts = content.get("timestamp_override") or "@timestamp"
        groups.setdefault((ordered, ts), []).append((key, _fields(content)))
    report.groups = len(groups)

    order = sorted(groups, key=lambda g: last_searched.get(g, float("-inf")))
    for n, group in enumerate(order):
        if not search_budget.spend(INTEGRATION, per_hour):
            report.deferred = len(order) - n
            break
        report.searched += 1
        last_searched[group] = time.monotonic()
        index, ts = ",".join(group[0]), group[1]
        members = groups[group]
        try:
            seen = await service.any_event_since(index, ts, DAYS)
        except Exception as exc:  # noqa: BLE001 -- the group reads unknown
            for key, _ in members:
                results[key] = _unknown(f"search of {index} failed: {exc}", index)
            continue
        present: Optional[set] = None
        caps_error = ""
        try:
            wanted = sorted({ts, *(f for _, fields in members for f in fields)})
            present = await service.field_caps(index, wanted)
        except Exception as exc:  # noqa: BLE001 -- only rules naming fields need it
            caps_error = str(exc)
        for key, fields in members:
            if not seen:
                unmapped = present is not None and ts not in present
                results[key] = {
                    "result": "fail",
                    "reason": f"fail: no events in {index} in {DAYS}d"
                    + (f" ({ts} is not in its mapping)" if unmapped else ""),
                    "data_source": index,
                }
            else:
                results[key] = _verdict(index, ts, fields, present, caps_error)
    return results


def _active_rules() -> List[Tuple[RuleKey, Mapping[str, Any]]]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRuleVersion.content,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(
                DetectionRule.source == kibana.SOURCE, DetectionRule.status == "active"
            )
            .order_by(DetectionRule.id)
        ).all()
    return [((rid, ver), content or {}) for rid, ver, content in rows]


def _write(results: Dict[RuleKey, Dict[str, Any]], source: str = kibana.SOURCE) -> int:
    """Store each result that differs from the version's stored Sees; runs is kept.

    ``source`` is the rules' (Kibana's, or Wazuh's for ``wazuh_sees``).
    A result or reason that differs from the rule's last recorded Sees, across
    versions, is also appended to detection_rule_check_changes.
    """
    from core.detections.inventory.sync import (
        _change,
        _last_results,
        _record_changes,
    )

    if not results:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with get_db_manager().session_scope() as session:
        last = _last_results(session, source)
        changes = []
        rows = session.execute(
            select(
                DetectionRuleVersion.rule_id,
                DetectionRuleVersion.version,
                DetectionRuleVersion.checks,
            ).where(
                tuple_(DetectionRuleVersion.rule_id, DetectionRuleVersion.version).in_(
                    list(results)
                )
            )
        ).all()
        changed = []
        for rid, ver, checks in rows:
            new, old = results[(rid, ver)], (checks or {}).get("sees") or {}
            seen = (new["result"], new["reason"])
            before = last.get((rid, "sees"))
            if before != seen:
                changes.append(
                    _change(rid, ver, "sees", before[0] if before else None, *seen)
                )
            if all(old.get(k) == v for k, v in new.items()):
                continue
            changed.append(
                {
                    "rid": rid,
                    "ver": ver,
                    "new_checks": {
                        **(checks or {}),
                        "sees": {**new, "checked_at": now},
                    },
                }
            )
        if changed:
            session.connection().execute(
                update(DetectionRuleVersion)
                .where(
                    DetectionRuleVersion.rule_id == bindparam("rid"),
                    DetectionRuleVersion.version == bindparam("ver"),
                )
                .values(checks=bindparam("new_checks")),
                changed,
            )
        _record_changes(session, changes, now)
    return len(changed)


def sweep_elastic(config: Mapping[str, Any]) -> SeesReport:
    """Check Sees for the active Kibana rules, as far as the hour's budget goes."""
    report = SeesReport()
    try:
        per_hour = config.get("max_searches_per_hour")
        per_hour = DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
        rules = _active_rules()
        report.rules = len(rules)
        service = kibana.service_of(config)

        async def _run() -> Dict[RuleKey, Dict[str, Any]]:
            try:
                return await _check(service, rules, per_hour, report)
            finally:
                await service.close()

        report.written = _write(asyncio.run(_run()))
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Sees sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Sees %s", report)
    return report
