"""Quiet: whether each active Kibana rule's alert volume stays under a threshold.

One size-0 search per sync through Kibana's signals route counts alerts per
rule per UTC day over the last 30 days, building blocks excluded. It is spent
against elastic-siem's ``max_searches_per_hour`` before Sees; when the budget
is spent Quiet is skipped and nothing is written. Never a second search.

A rule's window runs from its latest version's first_seen day (capped at 30
days) to now; under 24 h it reads ``unknown`` ("version too new"). Over
``quiet_max_alerts_per_day`` averaged over the window is ``fail``, else
``pass`` (a rule with no alerts too). Rules the terms aggregation could not
hold read ``unknown`` ("not counted this sync"); a failed search, ``unknown``
with its error.

The result goes under checks['quiet'] ({result, reason, count, days,
checked_at}), written only when it changed. Since the count moves the reason,
a change is recorded in detection_rule_check_changes only when the result
changes (or an unknown's reason). Nothing is written to Kibana.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, select

from core.detections.inventory import kibana
from core.detections.inventory.sees import INTEGRATION, RuleKey, write_check
from core.integrations._base import search_budget
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

DAYS = 30
DEFAULT_MAX_PER_DAY = 50
DEFAULT_PER_HOUR = 30
# Rules per terms aggregation: with up to 30 daily buckets each this stays
# under Elasticsearch's default search.max_buckets (65,536).
MAX_RULES = 2000
TOO_NEW = "version too new"
NOT_COUNTED = "not counted this sync"

Rule = Tuple[RuleKey, str, datetime]  # (rule id, version), rule_id, first_seen


@dataclass
class QuietReport:
    source: str = "elastic:quiet"
    rules: int = 0
    searched: int = 0
    written: int = 0
    skipped: Optional[str] = None
    error: Optional[str] = None


def _unknown(reason: str) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason, "count": None, "days": None}


def _rate(value: float) -> str:
    return f"{value:,.0f}" if value >= 10 else f"{value:.1f}".rstrip("0").rstrip(".")


def _verdict(count: int, days: int, max_per_day: int) -> Dict[str, Any]:
    rate = count / days
    text = f"{count:,} alert{'' if count == 1 else 's'} in {days} d ({_rate(rate)}/day)"
    if rate > max_per_day:
        return {
            "result": "fail",
            "reason": f"fail: {text}, over {max_per_day}/day",
            "count": count,
            "days": days,
        }
    return {"result": "pass", "reason": text, "count": count, "days": days}


def _results(
    rules: List[Rule], agg: Mapping[str, Any], max_per_day: int, now: datetime
) -> Dict[RuleKey, Dict[str, Any]]:
    """Each rule's verdict from the terms aggregation, over its own window."""
    days_of: Dict[str, Dict[str, int]] = {
        str(b["key"]): {
            d["key_as_string"][:10]: int(d["doc_count"])
            for d in (b.get("days") or {}).get("buckets") or []
        }
        for b in agg.get("buckets") or []
    }
    truncated = int(agg.get("sum_other_doc_count") or 0) > 0
    today = now.date()
    first_day = today - timedelta(days=DAYS - 1)
    out: Dict[RuleKey, Dict[str, Any]] = {}
    for key, rule_id, first_seen in rules:
        if now - first_seen < timedelta(hours=24):
            out[key] = _unknown(TOO_NEW)
            continue
        counted = days_of.get(rule_id)
        if counted is None and truncated:
            out[key] = _unknown(NOT_COUNTED)
            continue
        start = max(first_seen.astimezone(timezone.utc).date(), first_day)
        count = sum(n for day, n in (counted or {}).items() if day >= start.isoformat())
        # The count runs from the start day's midnight to now: whole days of
        # that, rounded, so a young version's rate is neither halved nor the
        # reason rewritten every sync.
        midnight = datetime.combine(start, datetime.min.time(), timezone.utc)
        days = max(round((now - midnight) / timedelta(days=1)), 1)
        out[key] = _verdict(count, days, max_per_day)
    return out


def _active_rules() -> List[Rule]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.native_key,
                DetectionRuleVersion.first_seen,
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
    return [((rid, ver), key, seen) for rid, ver, key, seen in rows]


def _logged(new: Mapping[str, Any], before: Optional[tuple]) -> bool:
    """A new result, or a new reason for an unknown: not every count that moved."""
    if before is None or before[0] != new["result"]:
        return True
    return new["result"] == "unknown" and before[1] != new["reason"]


def _int(value: Any, default: int) -> int:
    return default if value is None else max(int(value), 0)


def sweep_elastic(config: Mapping[str, Any]) -> QuietReport:
    """Count every active Kibana rule's alerts in one search, if the budget allows."""
    report = QuietReport()
    try:
        per_hour = _int(config.get("max_searches_per_hour"), DEFAULT_PER_HOUR)
        max_per_day = _int(config.get("quiet_max_alerts_per_day"), DEFAULT_MAX_PER_DAY)
        rules = _active_rules()
        report.rules = len(rules)
        if not rules:
            return report
        if not search_budget.spend(INTEGRATION, per_hour):
            report.skipped = "search budget spent"
            return report
        report.searched = 1
        service = kibana.service_of(config)
        ids = sorted({rule_id for _, rule_id, _ in rules})

        async def _run() -> Mapping[str, Any]:
            try:
                return await service.alerts_per_rule_per_day(
                    ids, DAYS, min(len(ids), MAX_RULES)
                )
            finally:
                await service.close()

        now = datetime.now(timezone.utc)
        try:
            results = _results(rules, asyncio.run(_run()), max_per_day, now)
        except Exception as exc:  # noqa: BLE001 -- every rule reads unknown
            reason = f"alert count failed: {exc}"
            results = {
                key: _unknown(TOO_NEW if now - seen < timedelta(hours=24) else reason)
                for key, _, seen in rules
            }
        report.written = write_check("quiet", results, _logged)
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Quiet failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Quiet %s", report)
    return report
