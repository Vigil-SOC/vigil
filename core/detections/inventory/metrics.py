"""The rollout's success metrics, read from what Vigil already stores.

Each section says where its numbers come from and, under ``unknown``, what the
stored data cannot say. Nothing is searched in a SIEM and nothing is written.

1. ``gap_to_shadow``: a confirmed gap is a proven Verdict an analyst concluded
   (hunt or Case, ``episodic_verdicts``) that no rule alerted on, reconstructed
   as ``identify_gaps``' runs_rules_missed does. It is closed by the first overlay
   rule declaring the technique (compared at parent level) whose Sigma form passed
   its replay against proven events (``checks.replay``) and whose copy entered
   ``shadow`` (``detection_rule_stages``) at or after the gap.
2. ``shadow_to_live``: per overlay copy and version, its first ``shadow`` row to
   its first ``live`` row (``detection_rule_stages``), with who promoted it and why.
3. ``live_health``: rules running somewhere (an active Kibana, Splunk or Wazuh
   rule, or an overlay copy Vigil runs at ``live``), one rule's copies counted
   once as the Detections page counts them, passing when every copy that runs
   passes Runs and Sees (latest version's ``checks``); and the Blind spots.
4. ``first_week``: the first 7 days after Vigil first read a SIEM rule
   (``detection_rule_versions.first_seen``), and the rules found silent (Silent
   fail), with no data (Sees fail) or never firing (Silent "no alerts in 30 d")
   in it, from ``detection_rule_check_changes``.
5. ``search_budget``: each SIEM integration's searches in the last hour
   (``siem_searches``) against its ``max_searches_per_hour``, and the throttle
   errors Vigil's own searches met (``detection_rule_check_changes`` reasons).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sqlalchemy import func, select, text

from core.detections.inventory import catalog, readers, rollout, splunk, sync
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleCheckChange,
    DetectionRuleStage,
    DetectionRuleVersion,
)

GAP_DAYS = 90
GAP_TARGET_HOURS = 24
LIVE_TARGET = 0.9
FIRST_WEEK_DAYS = 7
THROTTLE_DAYS = 30
SIEM_SOURCES = ("elastic", "splunk", "wazuh")
# The integrations whose searches spend a budget, and the limit when unset.
BUDGETED = {"elastic-siem": 30, "splunk": splunk.DEFAULT_PER_HOUR}
NEVER_FIRED_REASON = "no alerts in "
# How a SIEM says it refused a search for load or rate.
# Words inside a name (an index, sourcetype or field) are not the SIEM's words.
_ALONE = r"(?<![\w.-]){}(?![\w.-])"
THROTTLE = re.compile(
    "|".join(
        _ALONE.format(p)
        for p in (
            "429",
            "too many requests",
            r"rate[ -]limit(?:ed|s)?",
            r"throttl(?:ed|ing)",
            r"concurrent (?:historical )?searches",
            "search quota",
        )
    )
    + r"|es_rejected_execution_exception|circuit_breaking_exception",
    re.IGNORECASE,
)
# The data a check names (index=x, index x, sourcetype=y): never read as an error.
_NAMES = re.compile(
    r"\b(?:index|sourcetype|source|datamodel)\s*[=:]?\s*\S+", re.IGNORECASE
)


def throttle_error(reason: str) -> bool:
    """Whether a check's reason is a SIEM refusing a search for load or rate."""
    return bool(THROTTLE.search(_NAMES.sub(" ", reason)))


def _hours(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 3600, 1)


def _parent(technique: str) -> str:
    return technique.upper().split(".")[0]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _median(values: List[float]) -> Optional[float]:
    return round(median(values), 1) if values else None


# 1. Confirmed gap -> validated rule in shadow


def gap_to_shadow(
    gaps: Mapping[str, Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """Each gap technique and the first validated overlay rule in shadow after it.

    ``gaps``: technique -> {at, runs}. ``rules``: one per overlay rule, with
    name, techniques, validated and shadow_ats (every time a copy entered shadow).
    """
    rows = []
    for technique, gap in sorted(gaps.items(), key=lambda kv: kv[1]["at"]):
        at = gap["at"]
        closing = [
            (min(s for s in rule["shadow_ats"] if s >= at), rule["name"])
            for rule in rules
            if rule["validated"]
            and any(_parent(t) == _parent(technique) for t in rule["techniques"])
            and any(s >= at for s in rule["shadow_ats"])
        ]
        row: Dict[str, Any] = {
            "technique": technique,
            "gap_at": at.isoformat(),
            "runs": gap["runs"],
        }
        if closing:
            shadow_at, name = min(closing)
            row.update(
                rule=name, shadow_at=shadow_at.isoformat(), hours=_hours(at, shadow_at)
            )
        else:
            row.update(rule=None, open_hours=_hours(at, now))
        rows.append(row)
    hours = [r["hours"] for r in rows if "hours" in r]
    return {
        "gaps": len(rows),
        "closed": len(hours),
        "median_hours": _median(hours),
        "within_target": sum(1 for h in hours if h <= GAP_TARGET_HOURS),
        "target_hours": GAP_TARGET_HOURS,
        "rows": rows,
        "source": (
            "Gaps: proven hunt and Case Verdicts an analyst concluded in the last "
            f"{GAP_DAYS} days that no rule alerted on (episodic_verdicts, "
            "reconstructed against findings). Validated: the overlay rule's Sigma "
            "form passed its replay against proven events (checks.replay). In "
            "shadow: the copy's first shadow row at or after the gap "
            "(detection_rule_stages). Techniques compared at parent level."
        ),
        "unknown": [
            "Time waiting for a person (an approval, an analyst's review) is not "
            "stored, so it is included in each time.",
            "A gap concluded by the agent alone is not counted: only an analyst's "
            "Verdict confirms one.",
        ],
    }


def _gaps(days: int) -> Dict[str, Dict[str, Any]]:
    from core.detections.gaps import FINDINGS_LIMIT, read_ledger, reconstruct_verdict
    from core.storage.database_data_service import DatabaseDataService

    data = DatabaseDataService()

    def read(start: datetime, end: datetime) -> List[Dict[str, Any]]:
        return data.get_findings(
            limit=FINDINGS_LIMIT, timestamp_start=start, timestamp_end=end
        )

    gaps: Dict[str, Dict[str, Any]] = {}
    for v in read_ledger(days):
        if v["outcome"] != "proven" or v["trust"] != "analyst":
            continue
        reason, record = reconstruct_verdict(v, read)
        if reason or record.get("verdict") not in ("missed", "loglm"):
            continue
        at = _utc(v["concluded_at"])
        for technique in v["techniques"]:
            gap = gaps.setdefault(technique, {"at": at, "runs": 0})
            gap["at"] = min(gap["at"], at)
            gap["runs"] += 1
    return gaps


def _overlay_rules(session: Any) -> List[Dict[str, Any]]:
    copies = session.execute(
        select(
            DetectionRule.id,
            DetectionRule.name,
            DetectionRule.language,
            DetectionRule.techniques,
            DetectionRuleVersion.checks["replay"]["result"].astext,
        )
        .join(DetectionRuleVersion, catalog._latest_join())
        .where(DetectionRule.source == readers.OVERLAY_SOURCE)
    ).all()
    shadows: Dict[int, List[datetime]] = {}
    for rule_id, at in session.execute(
        select(DetectionRuleStage.rule_id, DetectionRuleStage.at).where(
            DetectionRuleStage.stage == "shadow"
        )
    ):
        shadows.setdefault(rule_id, []).append(_utc(at))
    # The overlay saves one rule's copies under one name.
    rules: Dict[str, Dict[str, Any]] = {}
    for rule_id, name, language, techniques, replay in copies:
        rule = rules.setdefault(
            name,
            {"name": name, "techniques": set(), "validated": False, "shadow_ats": []},
        )
        rule["techniques"] |= {t.upper() for t in techniques or []}
        rule["validated"] |= language == "sigma" and replay == "pass"
        rule["shadow_ats"] += shadows.get(rule_id, [])
    return list(rules.values())


# 2. Shadow -> live per copy and version


def shadow_to_live(
    stages: Iterable[Tuple[int, int, str, int, str, str, Optional[str], datetime]],
    now: datetime,
) -> Dict[str, Any]:
    """``stages``: (id, rule_id, name, version, stage, actor, reason, at).

    Paired in time order (at, then id); a rule's current stage is its last row by
    id, as the inventory reads it. A live row before the version's shadow row is
    no promotion.
    """
    rows = sorted(stages, key=lambda r: (_utc(r[7]), r[0]))
    first: Dict[Tuple[int, int], Dict[str, Any]] = {}
    current = {r[1]: (r[3], r[4]) for r in sorted(rows, key=lambda r: r[0])}
    for _, rule_id, name, version, stage, actor, reason, at in rows:
        at = _utc(at)
        key = (rule_id, version)
        if stage == "shadow" and key not in first:
            first[key] = {"rule_id": rule_id, "name": name, "version": version}
            first[key]["shadow_at"] = at
        elif (
            stage == "live"
            and key in first
            and "live_at" not in first[key]
            and at >= first[key]["shadow_at"]
        ):
            first[key].update(live_at=at, promoted_by=actor, reason=reason)
    promoted, waiting = [], []
    for (rule_id, version), row in first.items():
        shadow_at = row.pop("shadow_at")
        row["shadow_at"] = shadow_at.isoformat()
        if "live_at" in row:
            row["hours"] = _hours(shadow_at, row["live_at"])
            row["live_at"] = row["live_at"].isoformat()
            promoted.append(row)
        elif current.get(rule_id) == (version, "shadow"):
            row["in_shadow_hours"] = _hours(shadow_at, now)
            waiting.append(row)
    hours = [r["hours"] for r in promoted]
    return {
        "promoted": len(promoted),
        "median_hours": _median(hours),
        "promoted_by_vigil": sum(1 for r in promoted if r["promoted_by"] == "system"),
        "in_shadow": len(waiting),
        "exit_criteria": (
            f"{rollout.SHADOW_DAYS} days in shadow, every day's runs passed, no day "
            f"above {rollout.MAX_HITS_PER_DAY} hits, Sees and lint passed"
        ),
        "rows": promoted,
        "waiting": waiting,
        "source": (
            "Each overlay copy's stage rows (detection_rule_stages): a version's "
            "first shadow row to its first live row, with the actor (system is "
            "Vigil's own promotion on the exit criteria) and the reason."
        ),
        "unknown": [
            "Vigil serves one customer per install, so this is that customer's.",
        ],
    }


def _stages(session: Any) -> List[Tuple]:
    return session.execute(
        select(
            DetectionRuleStage.id,
            DetectionRuleStage.rule_id,
            DetectionRule.name,
            DetectionRuleStage.version,
            DetectionRuleStage.stage,
            DetectionRuleStage.actor,
            DetectionRuleStage.reason,
            DetectionRuleStage.at,
        )
        .join(DetectionRule, DetectionRule.id == DetectionRuleStage.rule_id)
        .order_by(DetectionRuleStage.at, DetectionRuleStage.id)
    ).all()


# 3. Live rules passing Runs and Sees everywhere they run, and the Blind spots


def live_health(
    copies: Iterable[Mapping[str, Any]], blind_spots: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """``copies``: each copy that runs, with its rule (the group it counts in),
    tool, runs and sees results."""
    rules: Dict[Any, bool] = {}
    by_tool: Dict[str, Dict[str, int]] = {}
    for copy in copies:
        passing = copy["runs"] == "pass" and copy["sees"] == "pass"
        rules[copy["rule"]] = rules.get(copy["rule"], True) and passing
        tally = by_tool.setdefault(copy["tool"], {"copies": 0, "passing": 0})
        tally["copies"] += 1
        tally["passing"] += passing
    passing = sum(rules.values())
    return {
        "live_rules": len(rules),
        "passing": passing,
        "share": round(passing / len(rules), 3) if rules else None,
        "target": LIVE_TARGET,
        "by_tool": by_tool,
        "blind_spots": len(blind_spots),
        "rules_starved": sum(len(s["rules"]) for s in blind_spots),
        "source": (
            "Rules running somewhere: active Kibana, Splunk and Wazuh rules, and "
            "overlay copies at live that no SIEM copy has taken over "
            "(detection_rules, detection_rule_stages). A SIEM copy linked to a repo "
            "or overlay rule counts with it, once. Passing: every one of its copies "
            "has Runs and Sees pass on its latest version (checks). Blind spots: "
            "the data sources an active rule fails Sees on."
        ),
        "unknown": [
            "A copy with no Runs or Sees result yet counts as not passing.",
        ],
    }


def _live_copies(session: Any) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(
            DetectionRule.id,
            DetectionRule.source,
            DetectionRule.language,
            catalog.RUNS.label("runs"),
            catalog.SEES.label("sees"),
            catalog.CURRENT_STAGE.c.stage,
        )
        .join(DetectionRuleVersion, catalog._latest_join())
        .outerjoin(
            catalog.CURRENT_STAGE,
            catalog.CURRENT_STAGE.c.rule_id == DetectionRule.id,
        )
        .where(catalog.ACTIVE)
    ).all()
    running = [
        r
        for r in rows
        if r.source in SIEM_SOURCES
        or (
            r.source == readers.OVERLAY_SOURCE
            and r.language in sync.STAGED_LANGUAGES
            and r.stage == "live"
        )
    ]
    taken_over = rollout.handed_off(
        [r.id for r in running if r.source == readers.OVERLAY_SOURCE]
    )
    rule_of = catalog._rule_of_copy(session)
    return [
        {
            "rule": rule_of.get(r.id, (r.id,)),
            "tool": catalog.tool_of(r.source),
            "runs": r.runs,
            "sees": r.sees,
        }
        for r in running
        if r.id not in taken_over
    ]


# 4. Silent, no-data and never-firing rules found in the first week


def first_week(
    start: Optional[datetime],
    changes: Iterable[Tuple[int, str, str, str, Optional[str], datetime]],
    now: datetime,
) -> Dict[str, Any]:
    """``changes``: (rule_id, name, check, new_result, reason, changed_at) of SIEM rules."""
    found: Dict[str, Dict[int, Dict[str, Any]]] = {
        "silent": {},
        "no_data": {},
        "never_firing": {},
    }
    first_at: Dict[str, datetime] = {}
    for rule_id, name, check, result, reason, at in changes:
        at = _utc(at)
        kind = None
        if check == "silent" and result == "fail":
            kind = "silent"
        elif check == "sees" and result == "fail":
            kind = "no_data"
        elif check == "silent" and (reason or "").startswith(NEVER_FIRED_REASON):
            kind = "never_firing"
        if kind is None:
            continue
        first_at[kind] = min(first_at.get(kind, at), at)
        if start and at < start + timedelta(days=FIRST_WEEK_DAYS):
            found[kind].setdefault(
                rule_id,
                {"rule_id": rule_id, "name": name, "at": at.isoformat()},
            )
    end = start + timedelta(days=FIRST_WEEK_DAYS) if start else None
    return {
        "started_at": start.isoformat() if start else None,
        "ends_at": end.isoformat() if end else None,
        "week_over": bool(end and now >= end),
        **{kind: len(rules) for kind, rules in found.items()},
        "first_found_day": {
            kind: (at - start).days + 1 for kind, at in first_at.items() if start
        },
        "rows": {kind: list(rules.values()) for kind, rules in found.items()},
        "source": (
            "The week starts when Vigil first read a Kibana, Splunk or Wazuh rule "
            "(detection_rule_versions.first_seen). A rule is found when a check "
            "change in that week (detection_rule_check_changes) set Silent to "
            "fail (silent), Sees to fail (no data), or Silent to 'no alerts in "
            "30 d' (never firing). first_found_day is the day each kind was first "
            "found, in or after the week."
        ),
        "unknown": [
            "Silent reads unknown for a rule in the inventory under 7 days, so "
            "silent and never-firing rules are first found from day 8.",
        ],
    }


def _siem_start(session: Any) -> Optional[datetime]:
    start = session.scalar(
        select(func.min(DetectionRuleVersion.first_seen))
        .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
        .where(DetectionRule.source.in_(SIEM_SOURCES))
    )
    return _utc(start) if start else None


def _siem_changes(session: Any, checks: Tuple[str, ...]) -> List[Tuple]:
    change = DetectionRuleCheckChange
    return session.execute(
        select(
            change.rule_id,
            DetectionRule.name,
            change.check_key,
            change.new_result,
            change.reason,
            change.changed_at,
        )
        .join(DetectionRule, DetectionRule.id == change.rule_id)
        .where(DetectionRule.source.in_(SIEM_SOURCES), change.check_key.in_(checks))
        .order_by(change.id)
    ).all()


# 5. Searches within budget, and throttle errors Vigil met


def search_budget(
    sent: Mapping[str, int],
    limits: Mapping[str, int],
    throttled: Iterable[Tuple[int, str, str, str, Optional[str], datetime]],
) -> Dict[str, Any]:
    """``sent``/``limits`` per integration; ``throttled``: (rule_id, tool, check,
    new_result, reason, changed_at) changes whose reason is a throttle."""
    integrations = {}
    total = within = 0
    for integration, limit in limits.items():
        count = sent.get(integration, 0)
        integrations[integration] = {
            "searches_last_hour": count,
            "max_searches_per_hour": limit,
        }
        total += count
        within += min(count, limit)
    events: Dict[Tuple, Dict[str, Any]] = {}
    for rule_id, tool, check, result, reason, at in throttled:
        # Vigil's own search failing reads unknown; a SIEM rule's own run
        # failing is the SIEM's, except for an overlay copy Vigil runs.
        if result != "unknown" and tool != catalog.OVERLAY:
            continue
        at = _utc(at)
        key = (tool, check, reason, at.replace(second=0, microsecond=0))
        event = events.setdefault(
            key,
            {
                "tool": tool,
                "check": check,
                "reason": reason,
                "at": at.isoformat(),
                "rules": 0,
            },
        )
        event["rules"] += 1
    return {
        "integrations": integrations,
        "share_within_budget": round(within / total, 3) if total else None,
        "throttle_errors": len(events),
        "throttle_rows": sorted(events.values(), key=lambda e: e["at"], reverse=True),
        "source": (
            "Searches: siem_searches rows in the last hour per integration, each "
            "recorded only after the budget allowed it, against the integration's "
            "max_searches_per_hour. Throttle errors: check changes in the last "
            f"{THROTTLE_DAYS} days (detection_rule_check_changes) where Vigil's own "
            "search read unknown, or an overlay copy's run failed, with a SIEM "
            "rate or load refusal as the reason; one per tool, check, reason and "
            "minute."
        ),
        "unknown": [
            "Searches older than an hour are not kept as counts, so the share "
            "covers the last hour only.",
            "A search the budget held back is not recorded, so it is not counted.",
        ],
    }


def _sent(session: Any) -> Dict[str, int]:
    rows = session.execute(text("""
            SELECT integration_id, count(*) FROM siem_searches
            WHERE searched_at > clock_timestamp() - interval '1 hour'
            GROUP BY integration_id
        """)).all()
    return {integration: count for integration, count in rows}


def _limits() -> Dict[str, int]:
    from core.config import load_integrations_config

    config = load_integrations_config()
    limits = {}
    for integration, default in BUDGETED.items():
        if integration not in config.get("enabled_integrations", []):
            continue
        per_hour = (config.get("integrations", {}).get(integration) or {}).get(
            "max_searches_per_hour"
        )
        limits[integration] = default if per_hour is None else max(int(per_hour), 0)
    return limits


def _throttled(session: Any, since: datetime) -> List[Tuple]:
    change = DetectionRuleCheckChange
    rows = session.execute(
        select(
            change.rule_id,
            DetectionRule.source,
            change.check_key,
            change.new_result,
            change.reason,
            change.changed_at,
        )
        .join(DetectionRule, DetectionRule.id == change.rule_id)
        .where(change.changed_at >= since, change.reason.isnot(None))
    ).all()
    return [
        (rule_id, catalog.tool_of(source), check, result, reason, at)
        for rule_id, source, check, result, reason, at in rows
        if throttle_error(reason)
    ]


def report(
    now: Optional[datetime] = None,
    read_gaps: Callable[[int], Dict[str, Dict[str, Any]]] = _gaps,
) -> Dict[str, Any]:
    """All five metrics, each with its source and what it cannot say."""
    now = now or datetime.now(timezone.utc)
    with get_db_manager().session_scope() as session:
        rules = _overlay_rules(session)
        stages = _stages(session)
        copies = _live_copies(session)
        start = _siem_start(session)
        changes = _siem_changes(session, ("silent", "sees"))
        sent = _sent(session)
        throttled = _throttled(session, now - timedelta(days=THROTTLE_DAYS))
    return {
        "generated_at": now.isoformat(),
        "gap_to_shadow": gap_to_shadow(read_gaps(GAP_DAYS), rules, now),
        "shadow_to_live": shadow_to_live(stages, now),
        "live_health": live_health(copies, catalog.blind_spots()),
        "first_week": first_week(start, changes, now),
        "search_budget": search_budget(sent, _limits(), throttled),
    }
