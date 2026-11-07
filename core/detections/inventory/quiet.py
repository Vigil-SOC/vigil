"""Quiet, Noisy and Silent: each active Kibana, Wazuh or Splunk rule's alert
volume, against a threshold, and (Kibana, Wazuh and Splunk) against its own history.

Per SIEM, one size-0 search per sync counts alerts per rule per UTC day over
the last 30 days: for Kibana through its signals route, building blocks
excluded; for Wazuh on ``wazuh-alerts-*`` on the indexer behind elastic-siem
(no Kibana needed), by ``rule.id``. Each is spent against elastic-siem's
``max_searches_per_hour`` (Kibana's before Sees, Wazuh's before Wazuh Sees);
when the budget is spent Quiet is skipped and nothing is written. Never a
second search. Both use elastic-siem's ``quiet_max_alerts_per_day``.

For Splunk, one search per sync (``SPLUNK_SPL``) counts each saved search's
triggered alerts (``index=_audit action=alert_fired``, its ``triggered_alerts``)
and, under Enterprise Security, its notables (``index=notable``); a search with
notables is counted by them alone, its triggered alerts being the same firings.
A search counted by neither is counted from the scheduler log
(``index=_internal sourcetype=scheduler``, successful runs whose
``alert_actions`` holds one outside Splunk's data-output actions,
``DATA_ACTIONS``, so not suppressed; ``fired`` each in per-result mode, else 1): an
untracked alert (``alert.track=0``) logs no ``alert_fired``. Counts are keyed as
the stored native_key (``app/name``, ``@owner`` when shared to its user); a name
that matches none is dropped. It is spent against the splunk integration's
``max_searches_per_hour`` after Runs and before Sees, and uses its own
``quiet_max_alerts_per_day``. When Vigil's Splunk user cannot search
``index=_audit`` every rule reads ``unknown``, never ``pass``, and nothing is spent;
when it cannot search the scheduler log (or it holds no events), every search
_audit and notables didn't count, a scheduled report aside, reads ``unknown``.

Wazuh shares Wazuh Sees' gate: when elastic-siem is off, has no
``elasticsearch_url``, or the indexer lists no ``wazuh-alerts-*`` index, every
active Wazuh rule reads ``unknown`` with that reason (its Noisy and Silent too),
and nothing is spent. A Wazuh rule below the manager's alert level is counted
like any other: with no alerts it reads ``pass``.

A rule's window runs from its latest version's first_seen day (capped at 30
days) to now; under 24 h it reads ``unknown`` ("version too new"). Over
``quiet_max_alerts_per_day`` averaged over the window is ``fail``, else
``pass`` (a rule with no alerts too). Rules the terms aggregation could not
hold read ``unknown`` ("not counted this sync"); a failed search, ``unknown``
with its error. When some Wazuh indices' shards fail (others answering), the
count is a floor: a ``fail`` stands, a ``pass`` reads ``unknown`` naming them;
Noisy reads ``unknown``, and so does a Silent ``fail`` or a ``days_fired`` of 0.

The result goes under checks['quiet'] ({result, reason, count, days,
checked_at}), written only when it changed. Since the count moves the reason,
a change is recorded in detection_rule_check_changes only when the result
changes (or an unknown's reason). Nothing is written to Kibana or the indexer.

Noisy and Silent (Kibana, Wazuh and Splunk rules) read the same per-rule
daily UTC buckets, never another search, with the thresholds of the rules' own
integration (elastic-siem's for Kibana and Wazuh, splunk's for Splunk). A Splunk
scheduled report (no correlation search, no ``alert.track``, and no ``actions``
but data-output ones, such as ``outputtelemetry`` or ``summary_index``) can't
alert: it reads both ``pass``, "a scheduled report; it does not alert", with no
``days_fired``, so it is no never-firing rule.
"Today" is the current UTC day's bucket so far (partial, so a spike reads late,
never early); "the last N d" is today and the N - 1 whole days before it.

- checks['noisy']: fail when today has at least ``noisy_min_alerts`` alerts and
  at least ``noisy_multiple`` times the daily mean of the 29 days before.
- checks['silent']: fail when the last ``silent_days`` have no alert and the
  days before them in the 30 had alerts on at least ``silent_min_active_days``
  distinct days. A rule that rarely fires is never silent. ``days_fired`` is how
  many of the 30 days had an alert: 0 is a never-firing rule.

Both are ``unknown`` when the rule (its first version, not its latest) has been
in the inventory under 7 days, was not counted this sync, or the search failed;
for Splunk too, with Quiet's reason, when index=_audit is unreadable or empty
(or, for a search _audit didn't count, the scheduler log).
A Wazuh rule below Wazuh's default alert level (3) with no alerts cannot alert:
once its search is read, both are ``pass`` and Silent carries no ``days_fired``,
so it is never counted as never-firing, whatever its age. One that has alerts
(a lowered log_alert_level) is judged on them.
Each is written like Quiet: a pass -> fail change row is a rule_health flag.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.orm import aliased

from core.detections.inventory import kibana, splunk, wazuh, wazuh_sees
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
REPORT = "a scheduled report; it does not alert"
DEFAULTS = {
    "noisy_min_alerts": 100,
    "noisy_multiple": 10,
    "silent_days": 7,
    "silent_min_active_days": 5,
}
# Wazuh's default log_alert_level: a rule below it never produces an alert.
WAZUH_ALERT_LEVEL = 3

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
    rules: List[Rule],
    days_of: Days,
    truncated: bool,
    max_per_day: int,
    now: datetime,
    failed: Tuple[str, ...] = (),
) -> Dict[RuleKey, Dict[str, Any]]:
    """Each rule's Quiet verdict, over its own window. With ``failed`` (indices
    whose shards failed) the count is a floor: a fail stands, a pass is unknown."""
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
        if failed and out[key]["result"] == "pass":
            out[key] = _unknown(
                f"{out[key]['reason']} in the indices that answered; "
                f"partial count, shards failed on {'; '.join(failed)}"
            )
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
    failed: Tuple[str, ...] = (),
) -> Tuple[Dict[RuleKey, Dict[str, Any]], Dict[RuleKey, Dict[str, Any]]]:
    """Each rule's Noisy and Silent verdicts from its daily buckets. With
    ``failed`` (indices whose shards failed) every bucket is a floor: Noisy is
    unknown, a Silent fail is unknown, and a days_fired of 0 is dropped."""
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
    if failed:
        partial = f"partial count, shards failed on {'; '.join(failed)}"
        for key in noisy:
            if noisy[key]["result"] != "unknown":
                reason = noisy[key]["reason"].removeprefix("fail: ")
                noisy[key] = _history_unknown(f"{reason}; {partial}")
            found = silent[key]
            # Alerts seen in the last days stand; a silence or a 0 might not.
            if found["result"] == "fail" or found.get("days_fired") == 0:
                reason = found["reason"].removeprefix("fail: ")
                kept = (
                    {"days_fired": found["days_fired"]}
                    if found.get("days_fired")
                    else {}
                )
                silent[key] = {**_history_unknown(f"{reason}; {partial}"), **kept}
    return noisy, silent


def _history_unknown(reason: str) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason}


def _history_failed(
    rules: List[Rule], reason: str, now: datetime
) -> Dict[RuleKey, Dict[str, Any]]:
    """No history read: every rule's Noisy and Silent unknown, or 'rule too new'."""
    return {
        key: _history_unknown(RULE_TOO_NEW if now - seen < MIN_AGE else reason)
        for key, _, _, seen in rules
    }


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
            noisy = _history_failed(rules, f"alert count failed: {exc}", now)
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


def _wazuh_levels(keys: List[RuleKey]) -> Dict[RuleKey, Any]:
    """Each Wazuh rule version's level, from its content as accurate reads it."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRuleVersion.rule_id,
                DetectionRuleVersion.version,
                DetectionRuleVersion.content["level"],
            ).where(
                tuple_(DetectionRuleVersion.rule_id, DetectionRuleVersion.version).in_(
                    keys
                )
            )
        ).all()
    return {(rid, ver): level for rid, ver, level in rows}


def _cannot_alert(
    rules: List[Rule],
    days_of: Days,
    truncated: bool,
    noisy: Dict[RuleKey, Dict[str, Any]],
    silent: Dict[RuleKey, Dict[str, Any]],
) -> None:
    """A rule below Wazuh's alert level with no alerts never alerts: Noisy and
    Silent pass, and no days_fired, so it is never counted as never-firing
    (whatever its age). A rule that did alert keeps its own verdicts."""
    exempt = {
        key
        for key, rule_id, _, _ in rules
        # Alerts (a lowered log_alert_level) are judged; with a truncated
        # count, absence is not "no alerts" and NOT_COUNTED stands.
        if not any((days_of.get(rule_id) or {}).values())
        and not (truncated and rule_id not in days_of)
    }
    for key, level in (_wazuh_levels(list(exempt)) if exempt else {}).items():
        # A missing or unreadable level gets no exemption: never guess.
        if type(level) is int and level < WAZUH_ALERT_LEVEL:
            reason = (
                f"level {level}, below Wazuh's alert level {WAZUH_ALERT_LEVEL}: "
                "never alerts"
            )
            noisy[key] = {"result": "pass", "reason": reason}
            silent[key] = {"result": "pass", "reason": reason}


def sweep_wazuh() -> QuietReport:
    """Count every active Wazuh rule's alerts in one indexer search, if Wazuh
    Sees' gate passes and the budget allows, and write Quiet, Noisy and Silent."""
    report = QuietReport(source="wazuh:quiet")
    try:
        rules = _active_rules(wazuh.SOURCE)
        report.rules = len(rules)
        if not rules:
            return report
        why, config = wazuh_sees.indexer()
        now = datetime.now(timezone.utc)
        results: Dict[RuleKey, Dict[str, Any]] = {}
        noisy: Dict[RuleKey, Dict[str, Any]] = {}
        silent: Dict[RuleKey, Dict[str, Any]] = {}
        if why is None:
            per_hour = _int(config.get("max_searches_per_hour"), DEFAULT_PER_HOUR)
            max_per_day = _int(
                config.get("quiet_max_alerts_per_day"), DEFAULT_MAX_PER_DAY
            )
            cfg = {k: _int(config.get(k), v) for k, v in DEFAULTS.items()}
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

            try:
                why, agg = asyncio.run(_run())
            except Exception as exc:  # noqa: BLE001 -- every rule reads unknown
                results = _failed(rules, exc, now)
                noisy = _history_failed(rules, f"alert count failed: {exc}", now)
                silent = dict(noisy)
            else:
                if why is None and agg is None:
                    report.skipped = "search budget spent"
                    return report
                if agg is not None:
                    days_of, truncated = _days(agg)
                    failed = tuple(agg.get("failed") or ())
                    results = _results(
                        rules, days_of, truncated, max_per_day, now, failed
                    )
                    noisy, silent = _history(
                        rules, days_of, truncated, cfg, now, failed
                    )
                    _cannot_alert(rules, days_of, truncated, noisy, silent)
        if why:
            results = {key: _unknown(why) for key, _, _, _ in rules}
            noisy = _history_failed(rules, why, now)
            silent = dict(noisy)
        report.written = sum(
            write_check(key, found, _logged, source=wazuh.SOURCE)
            for key, found in (("quiet", results), ("noisy", noisy), ("silent", silent))
        )
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Wazuh Quiet failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Wazuh Quiet %s", report)
    return report


# Splunk's data-output actions: they write results back into Splunk or to its
# telemetry endpoint and tell no one, so a search with only these is a report.
DATA_ACTIONS = (
    "summary_index",
    "summary_metric_index",
    "lookup",
    "populate_lookup",
    "outputtelemetry",
    "studio_snapshot",
)
_DATA_RE = "|".join(DATA_ACTIONS)

# One search: each saved search's alerts per UTC day (day = epoch days). From
# the triggered-alert audit (one event per firing; triggered_alerts results), the
# scheduler log (a run with an alert action outside DATA_ACTIONS:
# ``alert_actions=*`` matches "", hence the where; ``fired`` results in per-result mode, else 1;
# ``success`` only, so a search head cluster's delegation rows don't count it
# twice), the only one to see an untracked alert (alert.track=0) fire, and, under
# Enterprise Security, notables (one per result). A notable's owner and app are
# read from its orig_sid: each ``__plain`` or ``_<base64>`` (see _sid_part).
# The appended rows count each source index's events: none means it's unreadable.
SPLUNK_SPL = r"""search (index=_audit action=alert_fired ss_name=*)
  OR (index=notable search_name=* orig_sid=scheduler_*)
  OR (index=_internal sourcetype=scheduler status=success savedsearch_name=* alert_actions=*)
| eval acts=split(alert_actions, ",")
| eval acts=mvfilter(match(acts, "\S") AND NOT match(acts, "^\s*(DATA_RE)\s*$"))
| where index!="_internal" OR isnotnull(acts)
| rex field=orig_sid "^scheduler(?<sid_user>__[A-Za-z0-9]+|_[A-Za-z0-9+/=]+)(?<sid_app>__[A-Za-z0-9]+|_[A-Za-z0-9+/=]+)__"
| eval src=case(index="notable", "notable", index="_internal", "scheduler",
    true(), "audit"),
  name=case(src="notable", search_name, src="scheduler", savedsearch_name,
    true(), ss_name),
  user=case(src="notable", sid_user, src="scheduler", user, true(), ss_user),
  app=case(src="notable", sid_app, src="scheduler", app, true(), ss_app),
  n=case(src="notable", 1, src="scheduler", coalesce(tonumber(fired), 1),
    true(), coalesce(tonumber(triggered_alerts), 1)),
  day=floor(_time / 86400)
| stats sum(n) as n by src name user app day
| append [| tstats count as events
    where index=_audit OR (index=_internal sourcetype=scheduler) by index]""".replace(
    "DATA_RE", _DATA_RE
)


def _sid_part(part: str) -> str:
    """A sid's owner or app: ``__name`` as is, ``_<base64>`` decoded."""
    if part.startswith("__"):
        return part[2:]
    raw = part[1:]
    return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode()


def _splunk_days(rows: List[Mapping[str, Any]], keys: set) -> Tuple[Days, set]:
    """Per native_key (any stored, active or not) its alerts per UTC day, and the
    keys _audit or notables counted. A day with notables is counted by them alone:
    its triggered alerts are the same firings. The scheduler log counts only a key
    neither counted (an untracked alert): a tracked one's runs are its _audit
    firings. A row matching no stored key is dropped, never guessed."""
    counts: Dict[str, Days] = {"audit": {}, "notable": {}, "scheduler": {}}
    for row in rows:
        src = str(row.get("src") or "")
        if src not in counts:  # an index's event count
            continue
        user, app = str(row.get("user") or ""), str(row.get("app") or "")
        try:
            if src == "notable":
                user, app = _sid_part(user), _sid_part(app)
        except (ValueError, UnicodeDecodeError):
            continue
        name = row.get("name")
        # A user-shared search's key carries its owner (client.saved_search_key).
        key = next(
            (k for k in (f"{app}/{name}@{user}", f"{app}/{name}") if k in keys), None
        )
        if key is None:
            continue
        day = (date(1970, 1, 1) + timedelta(days=int(float(row["day"])))).isoformat()
        by_day = counts[src].setdefault(key, {})
        by_day[day] = by_day.get(day, 0) + int(float(row.get("n") or 0))
    days_of = counts["audit"]
    for key, by_day in counts["notable"].items():
        days_of[key] = {**days_of.get(key, {}), **by_day}
    audited = set(days_of)
    for key, by_day in counts["scheduler"].items():
        days_of.setdefault(key, by_day)
    return days_of, audited


def _native_keys(source: str) -> set:
    with get_db_manager().session_scope() as session:
        return set(
            session.scalars(
                select(DetectionRule.native_key).where(DetectionRule.source == source)
            )
        )


def _splunk_reports(rules: List[Rule]) -> set:
    """The rules whose latest version is a scheduled report: no correlation search,
    no tracked alert, and no alert action but DATA_ACTIONS, so nothing for it
    ever fires."""
    keys = [key for key, _, _, _ in rules]
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRuleVersion.rule_id,
                DetectionRuleVersion.version,
                DetectionRuleVersion.content,
            ).where(
                tuple_(DetectionRuleVersion.rule_id, DetectionRuleVersion.version).in_(
                    keys
                )
            )
        ).all()
    return {
        (rid, ver)
        for rid, ver, content in rows
        if not splunk._truthy(content.get("action.correlationsearch.enabled"))
        and not splunk._truthy(content.get("alert.track"))
        and not {a.strip() for a in str(content.get("actions") or "").split(",")}
        - {"", *DATA_ACTIONS}
    }


def sweep_splunk(config: Mapping[str, Any]) -> QuietReport:
    """Count every active Splunk saved search's alerts in one search, if Vigil's
    user can read index=_audit and the splunk budget allows, and write Quiet,
    Noisy and Silent from it. When index=_internal's scheduler log is unreadable,
    a search _audit and notables didn't count reads unknown."""
    report = QuietReport(source="splunk:quiet")
    try:
        max_per_day = _int(config.get("quiet_max_alerts_per_day"), DEFAULT_MAX_PER_DAY)
        cfg = {k: _int(config.get(k), v) for k, v in DEFAULTS.items()}
        rules = _active_rules(splunk.SOURCE)
        report.rules = len(rules)
        if not rules:
            return report
        service = splunk.service_of(config)
        now = datetime.now(timezone.utc)
        # No events from an unreadable index would read as a pass.
        scheduler_unreadable = (
            f"{service.username} cannot search index=_internal"
            if service.can_search_index("_internal") is False
            else None
        )
        if service.can_search_index("_audit") is False:
            why = f"{service.username} cannot search index=_audit"
            results = {key: _unknown(why) for key, _, _, _ in rules}
            noisy = {key: _history_unknown(why) for key, _, _, _ in rules}
            silent = dict(noisy)
        elif not search_budget.spend(splunk.SOURCE, splunk.per_hour_of(config)):
            report.skipped = "search budget spent"
            return report
        else:
            report.searched = 1
            first = now.date() - timedelta(days=DAYS - 1)
            earliest = datetime.combine(first, time.min, timezone.utc).timestamp()
            try:
                rows = service.search(
                    SPLUNK_SPL, earliest_time=str(int(earliest)), max_count=0
                ).results
                # Splunk returns no events, not an error, for an index the user
                # can't read (and can_search_index may not say).
                events = {
                    r.get("index"): int(r.get("events") or 0)
                    for r in rows
                    if "events" in r
                }
                if not events.get("_audit"):
                    why = "no events in index=_audit; it may not be readable"
                    results = {key: _unknown(why) for key, _, _, _ in rules}
                    noisy = {key: _history_unknown(why) for key, _, _, _ in rules}
                    silent = dict(noisy)
                else:
                    days_of, audited = _splunk_days(rows, _native_keys(splunk.SOURCE))
                    results = _results(rules, days_of, False, max_per_day, now)
                    noisy, silent = _history(rules, days_of, False, cfg, now)
                    # A report can't alert: no days_fired, so it isn't never-firing.
                    # One with counted alerts does alert, whatever its settings say.
                    fired = {key for key, rid, _, _ in rules if days_of.get(rid)}
                    reports = _splunk_reports(rules) - fired
                    for key in reports:
                        noisy[key] = {"result": "pass", "reason": REPORT}
                        silent[key] = {"result": "pass", "reason": REPORT}
                    # Without the scheduler log an untracked alert's firings go
                    # unseen: what _audit and notables didn't count is unknown.
                    unseen = scheduler_unreadable or (
                        not events.get("_internal")
                        and "no scheduler events in index=_internal; "
                        "it may not be readable"
                    )
                    if unseen:
                        for key, rid, _, _ in rules:
                            if rid in audited or key in reports:
                                continue
                            if results[key]["result"] != "unknown":
                                results[key] = _unknown(unseen)
                            if noisy[key]["result"] != "unknown":
                                noisy[key] = _history_unknown(unseen)
                            silent[key] = _history_unknown(
                                RULE_TOO_NEW
                                if silent[key]["reason"] == RULE_TOO_NEW
                                else unseen
                            )
            except Exception as exc:  # noqa: BLE001 -- every rule reads unknown
                results = _failed(rules, exc, now)
                noisy = _history_failed(rules, f"alert count failed: {exc}", now)
                silent = dict(noisy)
        report.written = sum(
            write_check(key, found, _logged, source=splunk.SOURCE)
            for key, found in (("quiet", results), ("noisy", noisy), ("silent", silent))
        )
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Splunk Quiet failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Splunk Quiet %s", report)
    return report
