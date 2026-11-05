"""Quiet, Noisy and Silent: each active Kibana or Wazuh rule's alert volume,
against a threshold, and (Kibana only) against its own history.

Per SIEM, one size-0 search per sync counts alerts per rule per UTC day over
the last 30 days: for Kibana through its signals route, building blocks
excluded; for Wazuh on ``wazuh-alerts-*`` on the indexer behind elastic-siem
(no Kibana needed), by ``rule.id``. Each is spent against elastic-siem's
``max_searches_per_hour`` (Kibana's before Sees, Wazuh's before Wazuh Sees);
when the budget is spent Quiet is skipped and nothing is written. Never a
second search. Both use elastic-siem's ``quiet_max_alerts_per_day``.

Wazuh shares Wazuh Sees' gate: when elastic-siem is off, has no
``elasticsearch_url``, or the indexer lists no ``wazuh-alerts-*`` index, every
active Wazuh rule reads ``unknown`` with that reason, and nothing is spent. A
Wazuh rule below the manager's alert level never alerts and reads ``pass``.

A rule's window runs from its latest version's first_seen day (capped at 30
days) to now; under 24 h it reads ``unknown`` ("version too new"). Over
``quiet_max_alerts_per_day`` averaged over the window is ``fail``, else
``pass`` (a rule with no alerts too). Rules the terms aggregation could not
hold read ``unknown`` ("not counted this sync"); a failed search, ``unknown``
with its error.

The result goes under checks['quiet'] ({result, reason, count, days,
checked_at}), written only when it changed. Since the count moves the reason,
a change is recorded in detection_rule_check_changes only when the result
changes (or an unknown's reason). Nothing is written to Kibana or the indexer.

Noisy and Silent (Kibana rules only, yet) read the same per-rule daily UTC buckets, never another search.
"Today" is the current UTC day's bucket so far (partial, so a spike reads late,
never early); "the last N d" is today and the N - 1 whole days before it.

- checks['noisy']: fail when today has at least ``noisy_min_alerts`` alerts and
  at least ``noisy_multiple`` times the daily mean of the 29 days before.
- checks['silent']: fail when the last ``silent_days`` have no alert and the
  days before them in the 30 had alerts on at least ``silent_min_active_days``
  distinct days. A rule that rarely fires is never silent. ``days_fired`` is how
  many of the 30 days had an alert: 0 is a never-firing rule.

Both are ``unknown`` when the rule (its first version, not its latest) has been
in the inventory under 7 days, was not counted this sync, or the search failed.
Each is written like Quiet: a pass -> fail change row is a rule_health flag.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from core.detections.inventory import kibana, wazuh, wazuh_sees
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
# Noisy and Silent need a week of the rule's own history.
MIN_AGE = timedelta(days=7)
RULE_TOO_NEW = "rule in the inventory under 7 d"
DEFAULTS = {
    "noisy_min_alerts": 100,
    "noisy_multiple": 10,
    "silent_days": 7,
    "silent_min_active_days": 5,
}

# (rule id, version), rule_id, version first_seen, rule first_seen
Rule = Tuple[RuleKey, str, datetime, datetime]
Days = Dict[str, Dict[str, int]]  # rule_id -> UTC day -> alerts


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


def _days(agg: Mapping[str, Any]) -> Tuple[Days, bool]:
    """Per rule_id its alerts per UTC day, and whether the aggregation truncated."""
    days_of: Days = {
        str(b["key"]): {
            d["key_as_string"][:10]: int(d["doc_count"])
            for d in (b.get("days") or {}).get("buckets") or []
        }
        for b in agg.get("buckets") or []
    }
    return days_of, int(agg.get("sum_other_doc_count") or 0) > 0


def _results(
    rules: List[Rule], days_of: Days, truncated: bool, max_per_day: int, now: datetime
) -> Dict[RuleKey, Dict[str, Any]]:
    """Each rule's Quiet verdict, over its own window."""
    today = now.date()
    first_day = today - timedelta(days=DAYS - 1)
    out: Dict[RuleKey, Dict[str, Any]] = {}
    for key, rule_id, first_seen, _ in rules:
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


def _noisy(by_day: List[int], cfg: Mapping[str, int]) -> Dict[str, Any]:
    today, prior = by_day[-1], by_day[:-1]
    mean = sum(prior) / len(prior)
    text = (
        f"{today:,} alert{'' if today == 1 else 's'} today (UTC, so far); "
        f"{_rate(mean)}/day over the {len(prior)} d before"
    )
    if today >= cfg["noisy_min_alerts"] and today >= cfg["noisy_multiple"] * mean:
        return {
            "result": "fail",
            "reason": f"fail: {text}, at least {cfg['noisy_min_alerts']} and "
            f"{cfg['noisy_multiple']}x the mean",
        }
    return {"result": "pass", "reason": text}


def _silent(by_day: List[int], cfg: Mapping[str, int]) -> Dict[str, Any]:
    quiet = min(max(cfg["silent_days"], 1), DAYS - 1)
    before = by_day[:-quiet]
    recent, active = sum(by_day[-quiet:]), sum(1 for n in before if n)
    fired = sum(1 for n in by_day if n)
    if fired == 0:
        return {"result": "pass", "reason": f"no alerts in {DAYS} d", "days_fired": 0}
    text = (
        f"{recent:,} alert{'' if recent == 1 else 's'} in the last {quiet} d; "
        f"alerts on {active} of the {len(before)} d before"
    )
    if recent == 0 and active >= cfg["silent_min_active_days"]:
        return {
            "result": "fail",
            "reason": f"fail: {text} (at least {cfg['silent_min_active_days']})",
            "days_fired": fired,
        }
    return {"result": "pass", "reason": text, "days_fired": fired}


def _history(
    rules: List[Rule],
    days_of: Days,
    truncated: bool,
    cfg: Mapping[str, int],
    now: datetime,
) -> Tuple[Dict[RuleKey, Dict[str, Any]], Dict[RuleKey, Dict[str, Any]]]:
    """Each rule's Noisy and Silent verdicts from its daily buckets."""
    today = now.date()
    window = [(today - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]
    noisy: Dict[RuleKey, Dict[str, Any]] = {}
    silent: Dict[RuleKey, Dict[str, Any]] = {}
    for key, rule_id, _, rule_seen in rules:
        counted = days_of.get(rule_id)
        if counted is None and truncated:
            noisy[key] = silent[key] = _history_unknown(NOT_COUNTED)
            continue
        by_day = [(counted or {}).get(day, 0) for day in window]
        if now - rule_seen < MIN_AGE:
            noisy[key] = _history_unknown(RULE_TOO_NEW)
            # Unjudged, but still counted for the never-firing tally.
            silent[key] = {
                **_history_unknown(RULE_TOO_NEW),
                "days_fired": sum(1 for n in by_day if n),
            }
            continue
        noisy[key], silent[key] = _noisy(by_day, cfg), _silent(by_day, cfg)
    return noisy, silent


def _history_unknown(reason: str) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason}


def _active_rules(source: str) -> List[Rule]:
    # The rule's first sighting in the inventory: its earliest version's.
    first = aliased(DetectionRuleVersion)
    rule_seen = (
        select(func.min(first.first_seen))
        .where(first.rule_id == DetectionRule.id)
        .scalar_subquery()
    )
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.native_key,
                DetectionRuleVersion.first_seen,
                rule_seen,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(DetectionRule.source == source, DetectionRule.status == "active")
            .order_by(DetectionRule.id)
        ).all()
    return [
        ((rid, ver), key, seen, rule_seen) for rid, ver, key, seen, rule_seen in rows
    ]


def _logged(new: Mapping[str, Any], before: Optional[tuple]) -> bool:
    """A new result, or a new reason for an unknown: not every count that moved."""
    if before is None or before[0] != new["result"]:
        return True
    return new["result"] == "unknown" and before[1] != new["reason"]


def _int(value: Any, default: int) -> int:
    return default if value is None else max(int(value), 0)


def _failed(
    rules: List[Rule], exc: Exception, now: datetime
) -> Dict[RuleKey, Dict[str, Any]]:
    """A failed search: every rule reads unknown with its error, or 'version too new'."""
    reason = f"alert count failed: {exc}"
    return {
        key: _unknown(TOO_NEW if now - seen < timedelta(hours=24) else reason)
        for key, _, seen, _ in rules
    }


def sweep_elastic(config: Mapping[str, Any]) -> QuietReport:
    """Count every active Kibana rule's alerts in one search, if the budget allows,
    and write Quiet, Noisy and Silent from it."""
    report = QuietReport()
    try:
        per_hour = _int(config.get("max_searches_per_hour"), DEFAULT_PER_HOUR)
        max_per_day = _int(config.get("quiet_max_alerts_per_day"), DEFAULT_MAX_PER_DAY)
        cfg = {k: _int(config.get(k), v) for k, v in DEFAULTS.items()}
        rules = _active_rules(kibana.SOURCE)
        report.rules = len(rules)
        if not rules:
            return report
        if not search_budget.spend(INTEGRATION, per_hour):
            report.skipped = "search budget spent"
            return report
        report.searched = 1
        service = kibana.service_of(config)
        ids = sorted({rule_id for _, rule_id, _, _ in rules})

        async def _run() -> Mapping[str, Any]:
            try:
                return await service.alerts_per_rule_per_day(
                    ids, DAYS, min(len(ids), MAX_RULES)
                )
            finally:
                await service.close()

        now = datetime.now(timezone.utc)
        try:
            days_of, truncated = _days(asyncio.run(_run()))
            results = _results(rules, days_of, truncated, max_per_day, now)
            noisy, silent = _history(rules, days_of, truncated, cfg, now)
        except Exception as exc:  # noqa: BLE001 -- every rule reads unknown
            results = _failed(rules, exc, now)
            noisy = {
                key: _history_unknown(
                    RULE_TOO_NEW
                    if now - seen < MIN_AGE
                    else f"alert count failed: {exc}"
                )
                for key, _, _, seen in rules
            }
            silent = dict(noisy)
        report.written = sum(
            write_check(key, found, _logged)
            for key, found in (("quiet", results), ("noisy", noisy), ("silent", silent))
        )
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Quiet failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Quiet %s", report)
    return report


def sweep_wazuh() -> QuietReport:
    """Count every active Wazuh rule's alerts in one indexer search, if Wazuh
    Sees' gate passes and the budget allows."""
    report = QuietReport(source="wazuh:quiet")
    try:
        rules = _active_rules(wazuh.SOURCE)
        report.rules = len(rules)
        if not rules:
            return report
        why, config = wazuh_sees.indexer()
        results: Dict[RuleKey, Dict[str, Any]] = {}
        if why is None:
            per_hour = _int(config.get("max_searches_per_hour"), DEFAULT_PER_HOUR)
            max_per_day = _int(
                config.get("quiet_max_alerts_per_day"), DEFAULT_MAX_PER_DAY
            )
            service = kibana.service_of(config)

            async def _run() -> Tuple[Optional[str], Optional[Mapping[str, Any]]]:
                """(why not read, or None; the aggregation, or None when skipped)."""
                try:
                    err, exists = await wazuh_sees.indices(
                        service, (wazuh_sees.ALERTS,)
                    )
                    if err or not exists[wazuh_sees.ALERTS]:
                        return (
                            err or f"no {wazuh_sees.ALERTS} index on the indexer",
                            None,
                        )
                    if not search_budget.spend(INTEGRATION, per_hour):
                        return None, None
                    report.searched = 1
                    # Few of the thousands of rules a manager loads alert: no include list.
                    return None, await service.alerts_per_rule_id_per_day(
                        wazuh_sees.ALERTS, DAYS, min(len(rules), MAX_RULES)
                    )
                finally:
                    await service.close()

            now = datetime.now(timezone.utc)
            try:
                why, agg = asyncio.run(_run())
            except Exception as exc:  # noqa: BLE001 -- every rule reads unknown
                results = _failed(rules, exc, now)
            else:
                if why is None and agg is None:
                    report.skipped = "search budget spent"
                    return report
                if agg is not None:
                    days_of, truncated = _days(agg)
                    results = _results(rules, days_of, truncated, max_per_day, now)
        if why:
            results = {key: _unknown(why) for key, _, _, _ in rules}
        report.written = write_check("quiet", results, _logged, source=wazuh.SOURCE)
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Wazuh Quiet failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Wazuh Quiet %s", report)
    return report
