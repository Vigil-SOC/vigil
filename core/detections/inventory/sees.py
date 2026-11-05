"""Sees: whether the data each active Kibana rule reads is arriving.

Wazuh rules get theirs from ``wazuh_sees`` and Splunk saved searches from
``splunk_sees``, each writing through ``write_check``.

A rule's data source is its ``index`` patterns (or its data view's) and the
timestamp field its 7-day range runs on. Rules on one source share one search:
a size-0 ``_search`` that stops at the first event, the only call counted
against elastic-siem's ``max_searches_per_hour``. ``_field_caps`` then says
whether the rule's ``required_fields`` are mapped. Groups are searched one at a
time, the least recently searched by any Vigil process first (never searched
before all); when the hour's budget is spent the rest wait for the next sweep.

Vigil's own Elastic overlay copies in shadow or live (not handed off to a
Kibana rule) are checked alongside, from their TOML's [rule] table, on the same
searches and budget; with no Kibana configured they are checked alone.

The result goes under checks['sees'] on the latest version, written only when
it changed. An ES|QL rule's indices are read from its leading ``FROM``; ML
rules, rules with no index and ES|QL with no readable ``FROM`` read
``unknown``, never ``pass``. Nothing is written to Elasticsearch or Kibana.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, bindparam, select, tuple_, update

from core.detections.inventory import kibana, readers
from core.integrations._base import search_budget
from core.integrations._base.search_params import validate_index
from core.integrations.elastic.descriptor import ELASTIC
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleStage, DetectionRuleVersion

logger = logging.getLogger(__name__)

DAYS = 7
DEFAULT_PER_HOUR = 30
INTEGRATION = ELASTIC.id

Key = Tuple[Tuple[str, ...], str]  # (patterns, timestamp field)
RuleKey = Tuple[int, int]  # (rule id, version)


def _budget_key(group: Key) -> str:
    """The group as its spend rows name it, for the shared oldest-first order."""
    return f"{','.join(group[0])}|{group[1]}"


@dataclass
class SeesReport:
    source: str = "elastic:sees"
    rules: int = 0
    groups: int = 0
    searched: int = 0
    deferred: int = 0
    written: int = 0
    closed: int = 0
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


ESQL_FROM = re.compile(r"FROM\s+(.+?)(?:\s+METADATA\s+.*)?", re.I | re.S)
ESQL_INDEX = re.compile(r"[^\s,|\"'`()\[\]]+")


def _esql_from(query: str) -> Tuple[List[str], Optional[str]]:
    """The indices of an ES|QL query's leading ``FROM a, b [METADATA ...]``."""
    head = re.sub(r"//[^\n]*", " ", query).split("|", 1)[0].strip()
    m = ESQL_FROM.fullmatch(head)
    if not m:
        return [], "ES|QL rule: its query does not start with FROM"
    patterns = [p.strip().strip('"') for p in m.group(1).split(",")]
    if not all(ESQL_INDEX.fullmatch(p) for p in patterns):
        return [], f"ES|QL rule: its FROM ({m.group(1).strip()}) could not be read"
    return patterns, None


async def _patterns(
    service: Any, content: Mapping[str, Any], views: Dict[str, Any]
) -> Tuple[List[str], Optional[str]]:
    """The rule's index patterns, or why there are none. A data view is read once a sweep."""
    kind = content.get("type")
    if kind == "esql":
        return _esql_from(str(content.get("query") or ""))
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

    last = search_budget.last_spent(INTEGRATION, map(_budget_key, groups))
    never = datetime.min.replace(tzinfo=timezone.utc)
    order = sorted(groups, key=lambda g: last.get(_budget_key(g), never))
    for n, group in enumerate(order):
        if not search_budget.spend(INTEGRATION, per_hour, _budget_key(group)):
            report.deferred = len(order) - n
            break
        report.searched += 1
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
        denied: List[str] = []
        if not seen:
            # Security trims a wildcard to the indices this user may read, so a
            # missing role looks like no events. Metadata: no budget spent.
            try:
                denied = await service.unreadable(list(group[0]))
            except Exception as exc:  # noqa: BLE001 -- can't tell: keep the fail
                logger.debug("sees: read privilege check of %s failed: %s", index, exc)
        for key, fields in members:
            if denied:
                results[key] = {
                    **_unknown(
                        f"Vigil's Elastic user lacks read on {', '.join(denied)}, "
                        "so whether its events arrive can't be seen",
                        index,
                    ),
                    "unreadable": True,  # rollout holds an overlay copy's runs
                }
            elif not seen:
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


STAGEFUL = ("shadow", "live")


def _overlay_rows(session: Any, language: str) -> List[tuple]:
    """(id, latest version, content, checks, current stage) of the active overlay
    copies of ``language`` that have a stage."""
    s = DetectionRuleStage
    current = (
        select(s.rule_id, s.stage)
        .distinct(s.rule_id)
        .order_by(s.rule_id, s.at.desc(), s.id.desc())
        .subquery()
    )
    return session.execute(
        select(
            DetectionRule.id,
            DetectionRule.latest_version,
            DetectionRuleVersion.content,
            DetectionRuleVersion.checks,
            current.c.stage,
        )
        .join(
            DetectionRuleVersion,
            and_(
                DetectionRuleVersion.rule_id == DetectionRule.id,
                DetectionRuleVersion.version == DetectionRule.latest_version,
            ),
        )
        .join(current, current.c.rule_id == DetectionRule.id)
        .where(
            DetectionRule.source == readers.OVERLAY_SOURCE,
            DetectionRule.language == language,
            DetectionRule.status == "active",
        )
        .order_by(DetectionRule.id)
    ).all()


def overlay_copies(language: str, siem: str) -> List[Tuple[RuleKey, Mapping[str, Any]]]:
    """Active overlay copies of ``language`` at their latest version whose current
    stage is shadow or live, less those an active SIEM copy runs instead
    (``rollout.handed_off``: a ``siem`` rule, since ``language`` picks it)."""
    from core.detections.inventory.rollout import handed_off

    with get_db_manager().session_scope() as session:
        rows = [r for r in _overlay_rows(session, language) if r[4] in STAGEFUL]
    gone = handed_off([r[0] for r in rows])
    return [
        ((rid, ver), content or {})
        for rid, ver, content, _, _ in rows
        if rid not in gone
    ]


def close_unchecked(language: str, selected: set) -> int:
    """Close Sees on the overlay copies of ``language`` that ``overlay_copies`` left
    out (``selected`` is what it returned): a NULL row and the key dropped, for
    each with a non-NULL Sees last recorded. Returns how many were closed."""
    from core.detections.inventory.rollout import handed_off
    from core.detections.inventory.sync import (
        _dropped,
        _last_results,
        _record_changes,
        _write_checks,
    )

    siem = "Splunk" if language == "splunk" else "Kibana"
    with get_db_manager().session_scope() as session:
        last = _last_results(session, readers.OVERLAY_SOURCE)
        rows = [
            r
            for r in _overlay_rows(session, language)
            if r[0] not in selected and last.get((r[0], "sees"), (None,))[0] is not None
        ]
        gone = handed_off([r[0] for r in rows if r[4] in STAGEFUL])
        changes, checked = [], []
        for rid, ver, _, checks, stage in rows:
            if stage in STAGEFUL and rid not in gone:
                continue  # stageful since overlay_copies read it: the next sweep checks it
            reason = (
                f"checked on {siem}'s copy"
                if rid in gone
                else f"not checked at stage {stage}"
            )
            dropped, kept = _dropped(rid, ver, checks, ("sees",), last, reason)
            changes += dropped
            if kept != (checks or {}):
                checked.append((rid, ver, kept))
        _write_checks(session, checked)
        _record_changes(session, changes, datetime.now(timezone.utc).isoformat())
    return len(changes)


def _overlay_rule(content: Mapping[str, Any]) -> Dict[str, Any]:
    """An Elastic copy's [rule] table, read as a Kibana rule's content is."""
    from core.detections.inventory.rollout import _toml_rule

    rule = dict(_toml_rule(content))
    if isinstance(rule.get("index"), str):
        rule["index"] = [rule["index"]]
    if rule.get("language") == "esql":
        rule["type"] = "esql"
    return rule


def _logged(new: Mapping[str, Any], before: Optional[tuple]) -> bool:
    return before != (new["result"], new["reason"])


def write_check(
    key: str,
    results: Dict[RuleKey, Dict[str, Any]],
    logged: Callable[[Mapping[str, Any], Optional[tuple]], bool] = _logged,
    source: str = kibana.SOURCE,
) -> int:
    """Store each result that differs from the version's stored checks[key]; the
    other checks are kept.

    A result for which ``logged(new, last recorded (result, reason))`` holds,
    across versions, is also appended to detection_rule_check_changes, in the
    same transaction. By default: a result or reason that differs.
    ``source`` is the rules' (Kibana's, or Wazuh's / Splunk's for their Sees).
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
            new, old = results[(rid, ver)], (checks or {}).get(key) or {}
            before = last.get((rid, key))
            if logged(new, before):
                changes.append(
                    _change(
                        rid,
                        ver,
                        key,
                        before[0] if before else None,
                        new["result"],
                        new["reason"],
                    )
                )
            if all(old.get(k) == v for k, v in new.items()):
                continue
            changed.append(
                {
                    "rid": rid,
                    "ver": ver,
                    "new_checks": {
                        **(checks or {}),
                        key: {**new, "checked_at": now},
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


def sweep_elastic(config: Mapping[str, Any], kibana_rules: bool = True) -> SeesReport:
    """Check Sees for the active Kibana rules (unless ``kibana_rules`` is False)
    and the stageful Elastic overlay copies, as far as the hour's budget goes."""
    report = SeesReport()
    try:
        per_hour = config.get("max_searches_per_hour")
        per_hour = DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
        rules = _active_rules() if kibana_rules else []
        try:
            copies = [
                (key, _overlay_rule(content))
                for key, content in overlay_copies("elastic", kibana.SOURCE)
            ]
        except Exception as exc:  # noqa: BLE001 -- Kibana's rules still get Sees
            logger.warning("detection rules: overlay copies unread for Sees: %s", exc)
            report.error = f"overlay: {type(exc).__name__}: {exc}"
            copies = []
        else:  # left-out copies close only when the selection was read
            report.closed = close_unchecked("elastic", {k[0] for k, _ in copies})
        report.rules = len(rules) + len(copies)
        service = kibana.service_of(config)

        async def _run() -> Dict[RuleKey, Dict[str, Any]]:
            try:
                return await _check(service, rules + copies, per_hour, report)
            finally:
                await service.close()

        results = asyncio.run(_run())
        # One write per source: write_check reads the last results per source.
        report.written = write_check(
            "sees", {k: results[k] for k, _ in rules if k in results}
        ) + write_check(
            "sees",
            {k: results[k] for k, _ in copies if k in results},
            source=readers.OVERLAY_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Sees sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Sees %s", report)
    return report
