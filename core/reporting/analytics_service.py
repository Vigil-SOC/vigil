"""Analytics queries behind the /analytics endpoints.

Aggregations over findings, cases, and LLM interaction logs. Callers pass the
request-scoped ``Session``; nothing here commits — the unit of work owns that.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from core.llm.providers.registry import get_registry, infer_provider_type
from core.storage.models import Case, CaseClosureInfo, Finding, LLMInteractionLog
from core.threat_intel.mitre_lookup import get_time_range, resolve_technique

logger = logging.getLogger(__name__)


async def collect_insights_inputs(
    db: Session, time_range: str
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Collect the metrics + time_series inputs that the insights model needs.

    Kept small so both the GET (opportunistic background refresh) and POST
    (forced refresh) insights endpoints can schedule regeneration without
    duplicating logic.
    """
    start_time, end_time = get_time_range(time_range)
    period_duration = end_time - start_time
    prev_start = start_time - period_duration
    metrics = await calculate_metrics(db, start_time, end_time, prev_start, start_time)
    time_series = await get_time_series_data(db, start_time, end_time, time_range)
    return metrics, time_series


async def calculate_metrics(
    db: Session,
    start_time: datetime,
    end_time: datetime,
    prev_start: datetime,
    prev_end: datetime,
) -> Dict[str, Any]:
    """Calculate key SOC metrics and their period-over-period changes."""

    # Current period metrics
    total_findings = (
        db.query(func.count(Finding.finding_id))
        .filter(Finding.created_at.between(start_time, end_time))
        .scalar()
        or 0
    )

    total_cases = (
        db.query(func.count(Case.case_id))
        .filter(Case.created_at.between(start_time, end_time))
        .scalar()
        or 0
    )

    # Get average response time (time from case creation to first analyst interaction)
    avg_response_time_result = (
        db.query(
            func.avg(func.extract("epoch", Case.updated_at - Case.created_at) / 60)
        )
        .filter(
            and_(Case.created_at.between(start_time, end_time), Case.status != "new")
        )
        .scalar()
    )

    avg_response_time = float(round(avg_response_time_result or 0, 1))

    # Calculate false positive rate
    total_closed = (
        db.query(func.count(Case.case_id))
        .filter(
            and_(Case.created_at.between(start_time, end_time), Case.status == "closed")
        )
        .scalar()
        or 0
    )

    false_positives = (
        db.query(func.count(CaseClosureInfo.case_id))
        .join(Case, Case.case_id == CaseClosureInfo.case_id)
        .filter(
            and_(
                Case.created_at.between(start_time, end_time),
                CaseClosureInfo.closure_category == "false_positive",
            )
        )
        .scalar()
        or 0
    )

    false_positive_rate = round(
        (false_positives / total_closed * 100) if total_closed > 0 else 0, 1
    )

    # Previous period metrics for comparison
    prev_total_findings = (
        db.query(func.count(Finding.finding_id))
        .filter(Finding.created_at.between(prev_start, prev_end))
        .scalar()
        or 0
    )

    prev_total_cases = (
        db.query(func.count(Case.case_id))
        .filter(Case.created_at.between(prev_start, prev_end))
        .scalar()
        or 0
    )

    prev_avg_response_time_result = (
        db.query(
            func.avg(func.extract("epoch", Case.updated_at - Case.created_at) / 60)
        )
        .filter(
            and_(Case.created_at.between(prev_start, prev_end), Case.status != "new")
        )
        .scalar()
    )

    # float() to match avg_response_time above — SQL AVG() returns Decimal, and
    # subtracting a float from a Decimal (when the previous period has data but
    # the current one doesn't) raises TypeError. This is why 7d 500s while 30d,
    # whose previous window is usually empty, short-circuits the subtraction.
    prev_avg_response_time = float(round(prev_avg_response_time_result or 0, 1))

    prev_total_closed = (
        db.query(func.count(Case.case_id))
        .filter(
            and_(Case.created_at.between(prev_start, prev_end), Case.status == "closed")
        )
        .scalar()
        or 0
    )

    prev_false_positives = (
        db.query(func.count(CaseClosureInfo.case_id))
        .join(Case, Case.case_id == CaseClosureInfo.case_id)
        .filter(
            and_(
                Case.created_at.between(prev_start, prev_end),
                CaseClosureInfo.closure_category == "false_positive",
            )
        )
        .scalar()
        or 0
    )

    prev_false_positive_rate = round(
        (
            (prev_false_positives / prev_total_closed * 100)
            if prev_total_closed > 0
            else 0
        ),
        1,
    )

    # Calculate percentage changes
    findings_change = round(
        (
            ((total_findings - prev_total_findings) / prev_total_findings * 100)
            if prev_total_findings > 0
            else 0
        ),
        1,
    )

    cases_change = round(
        (
            ((total_cases - prev_total_cases) / prev_total_cases * 100)
            if prev_total_cases > 0
            else 0
        ),
        1,
    )

    response_time_change = round(
        (
            (
                (avg_response_time - prev_avg_response_time)
                / prev_avg_response_time
                * 100
            )
            if prev_avg_response_time > 0
            else 0
        ),
        1,
    )

    false_positive_change = round(false_positive_rate - prev_false_positive_rate, 1)

    return {
        "totalFindings": total_findings,
        "totalCases": total_cases,
        "avgResponseTime": avg_response_time,
        "falsePositiveRate": false_positive_rate,
        "findingsChange": findings_change,
        "casesChange": cases_change,
        "responseTimeChange": response_time_change,
        "falsePositiveChange": false_positive_change,
    }


async def get_time_series_data(
    db: Session, start_time: datetime, end_time: datetime, time_range: str
) -> List[Dict[str, Any]]:
    """Get time series data for findings, cases, and alerts."""

    # Determine bucket size based on time range
    if time_range == "24h":
        bucket_size = timedelta(hours=1)
        bucket_count = 24
    elif time_range == "7d":
        bucket_size = timedelta(hours=6)
        bucket_count = 28
    else:  # 30d
        bucket_size = timedelta(days=1)
        bucket_count = 30

    time_series = []
    current_time = start_time

    for _ in range(bucket_count):
        bucket_end = min(current_time + bucket_size, end_time)

        findings_count = (
            db.query(func.count(Finding.finding_id))
            .filter(Finding.created_at.between(current_time, bucket_end))
            .scalar()
            or 0
        )

        cases_count = (
            db.query(func.count(Case.case_id))
            .filter(Case.created_at.between(current_time, bucket_end))
            .scalar()
            or 0
        )

        # Alerts are high/critical severity findings
        alerts_count = (
            db.query(func.count(Finding.finding_id))
            .filter(
                and_(
                    Finding.created_at.between(current_time, bucket_end),
                    Finding.severity.in_(["high", "critical"]),
                )
            )
            .scalar()
            or 0
        )

        time_series.append(
            {
                "timestamp": current_time.isoformat(),
                "findings": findings_count,
                "cases": cases_count,
                "alerts": alerts_count,
            }
        )

        current_time = bucket_end

    return time_series


async def get_severity_distribution(
    db: Session, start_time: datetime, end_time: datetime
) -> List[Dict[str, Any]]:
    """Get distribution of findings by severity."""

    severity_colors = {
        "critical": "#d32f2f",
        "high": "#f57c00",
        "medium": "#fbc02d",
        "low": "#388e3c",
        "informational": "#757575",
    }

    severity_counts = (
        db.query(Finding.severity, func.count(Finding.finding_id).label("count"))
        .filter(Finding.created_at.between(start_time, end_time))
        .group_by(Finding.severity)
        .all()
    )

    return [
        {
            "name": severity.capitalize() if severity else "Unknown",
            "value": count,
            "color": severity_colors.get(severity, "#757575"),
        }
        for severity, count in severity_counts
    ]


async def get_top_alert_sources(
    db: Session, start_time: datetime, end_time: datetime, limit: int = 10
) -> List[Dict[str, Any]]:
    """Get top alert sources by finding count."""

    top_sources = (
        db.query(Finding.data_source, func.count(Finding.finding_id).label("count"))
        .filter(Finding.created_at.between(start_time, end_time))
        .group_by(Finding.data_source)
        .order_by(func.count(Finding.finding_id).desc())
        .limit(limit)
        .all()
    )

    return [
        {"name": source or "Unknown", "count": count} for source, count in top_sources
    ]


async def get_response_time_trend(
    db: Session, start_time: datetime, end_time: datetime, time_range: str
) -> List[Dict[str, Any]]:
    """Get response time trend over the period."""

    # Determine periods based on time range
    if time_range == "24h":
        period_size = timedelta(hours=4)
        period_count = 6
    elif time_range == "7d":
        period_size = timedelta(days=1)
        period_count = 7
    else:  # 30d
        period_size = timedelta(days=5)
        period_count = 6

    trend_data = []
    current_time = start_time
    target_time = 30  # 30 minute target response time

    for i in range(period_count):
        period_end = min(current_time + period_size, end_time)

        avg_time_result = (
            db.query(
                func.avg(func.extract("epoch", Case.updated_at - Case.created_at) / 60)
            )
            .filter(
                and_(
                    Case.created_at.between(current_time, period_end),
                    Case.status != "new",
                )
            )
            .scalar()
        )

        avg_time = round(avg_time_result or 0, 1)

        trend_data.append(
            {
                "period": f"P{i+1}",
                "avgTime": avg_time,
                "target": target_time,
            }
        )

        current_time = period_end

    return trend_data


async def get_affected_entities(
    db: Session, start_time: datetime, end_time: datetime, limit: int = 15
) -> List[Dict[str, Any]]:
    """Get top affected entities/devices from findings."""

    findings = (
        db.query(Finding).filter(Finding.created_at.between(start_time, end_time)).all()
    )

    entity_counts = {}
    entity_severities = {}

    for finding in findings:
        if not finding.entity_context:
            continue

        # Extract entities from entity_context
        entities = []
        ctx = finding.entity_context

        # Common entity types
        if isinstance(ctx, dict):
            # Network entities
            for key in [
                "hostname",
                "host",
                "device",
                "src_ip",
                "dst_ip",
                "dest_ip",
                "ip_address",
                "src_host",
                "dst_host",
            ]:
                if key in ctx and ctx[key]:
                    value = ctx[key]
                    if value and value != "null":  # Skip null values
                        entities.append(str(value))

            # User entities
            for key in ["username", "user", "user_id", "account"]:
                if key in ctx and ctx[key]:
                    value = ctx[key]
                    if value and value != "null":
                        entities.append(str(value))

        for entity in entities:
            if entity not in entity_counts:
                entity_counts[entity] = 0
                entity_severities[entity] = {
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                }

            entity_counts[entity] += 1
            severity = finding.severity or "low"
            if severity in entity_severities[entity]:
                entity_severities[entity][severity] += 1

    # Convert to list and sort by count
    entities_list = [
        {
            "entity": entity,
            "count": count,
            "critical": entity_severities[entity]["critical"],
            "high": entity_severities[entity]["high"],
            "medium": entity_severities[entity]["medium"],
            "low": entity_severities[entity]["low"],
            "riskScore": (
                entity_severities[entity]["critical"] * 10
                + entity_severities[entity]["high"] * 5
                + entity_severities[entity]["medium"] * 2
                + entity_severities[entity]["low"]
            ),
        }
        for entity, count in entity_counts.items()
    ]

    # Sort by risk score
    entities_list.sort(key=lambda x: x["riskScore"], reverse=True)

    return entities_list[:limit]


async def get_attack_time_heatmap(
    db: Session, start_time: datetime, end_time: datetime
) -> List[Dict[str, Any]]:
    """Get attack time heatmap data (hour of day x day of week)."""

    findings = (
        db.query(Finding).filter(Finding.created_at.between(start_time, end_time)).all()
    )

    # Initialize heatmap grid (7 days x 24 hours)
    heatmap = {}
    for day in range(7):  # 0 = Monday, 6 = Sunday
        for hour in range(24):
            key = f"{day}:{hour}"
            heatmap[key] = {"count": 0, "critical": 0, "high": 0}

    # Populate heatmap
    for finding in findings:
        timestamp = finding.timestamp
        day_of_week = timestamp.weekday()  # 0 = Monday
        hour = timestamp.hour

        key = f"{day_of_week}:{hour}"
        heatmap[key]["count"] += 1

        if finding.severity == "critical":
            heatmap[key]["critical"] += 1
        elif finding.severity == "high":
            heatmap[key]["high"] += 1

    # Convert to list format
    heatmap_data = []
    day_names = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]

    for day in range(7):
        for hour in range(24):
            key = f"{day}:{hour}"
            heatmap_data.append(
                {
                    "day": day_names[day],
                    "dayNum": day,
                    "hour": hour,
                    "count": heatmap[key]["count"],
                    "critical": heatmap[key]["critical"],
                    "high": heatmap[key]["high"],
                    "intensity": heatmap[key]["count"],  # For heatmap color intensity
                }
            )

    return heatmap_data


async def get_mitre_technique_distribution(
    db: Session, start_time: datetime, end_time: datetime, limit: int = 10
) -> List[Dict[str, Any]]:
    """Get distribution of MITRE ATT&CK techniques from findings."""

    findings = (
        db.query(Finding).filter(Finding.created_at.between(start_time, end_time)).all()
    )

    technique_counts: dict[str, int] = {}
    technique_meta: dict[str, tuple[str, str]] = {}

    def _record(tech):
        tid, name, tactic = resolve_technique(tech)
        if not tid:
            return
        technique_counts[tid] = technique_counts.get(tid, 0) + 1
        if tid not in technique_meta:
            technique_meta[tid] = (name, tactic)

    for finding in findings:
        if not finding.mitre_predictions:
            continue

        predictions = finding.mitre_predictions

        if isinstance(predictions, dict):
            if predictions and all(
                isinstance(v, (int, float)) for v in predictions.values()
            ):
                # Standard format: {tactic_or_technique_id: confidence}
                for tech_id in predictions.keys():
                    _record(tech_id)
            else:
                if "techniques" in predictions:
                    nested = predictions["techniques"]
                elif "predicted_techniques" in predictions:
                    nested = predictions["predicted_techniques"]
                else:
                    nested = [predictions]

                for tech in nested:
                    if isinstance(tech, dict):
                        _record(tech)
        elif isinstance(predictions, list):
            for tech in predictions:
                if isinstance(tech, dict):
                    _record(tech)

    techniques_list = [
        {
            "techniqueId": tech_id,
            "techniqueName": technique_meta[tech_id][0],
            "tactic": technique_meta[tech_id][1],
            "count": count,
        }
        for tech_id, count in technique_counts.items()
    ]

    techniques_list.sort(key=lambda x: x["count"], reverse=True)

    return techniques_list[:limit]


def _cost_time_series_from_bifrost(start_time, end_time) -> Optional[Dict[str, Any]]:
    """Pull time-bucketed cost from Bifrost's logging API (#185).

    Returns the ``buckets``/``bucket_size_seconds``/``models`` payload
    as-is so the frontend can chart it directly. Returns ``None`` on any
    failure (Bifrost down, log plugin off, network) so the dashboard
    keeps working with the local aggregations.
    """
    try:
        from core.llm.bifrost.costs import histogram_cost

        return histogram_cost(
            start_time=start_time.isoformat() if start_time else None,
            end_time=end_time.isoformat() if end_time else None,
        )
    except Exception:
        return None


def _cache_hit_rate(input_tokens: int, cache_read_tokens: int) -> float:
    """Fraction of prompt-side tokens served from Anthropic's cache.

    Denominator is `input_tokens + cache_read_tokens` — Anthropic reports
    uncached input and cached reads as disjoint counters.
    """
    denom = input_tokens + cache_read_tokens
    if denom <= 0:
        return 0.0
    return round(cache_read_tokens / denom, 4)


def _cost_totals(db: Session, base_filter) -> Dict[str, Any]:
    row = (
        db.query(
            func.coalesce(func.sum(LLMInteractionLog.input_tokens), 0),
            func.coalesce(func.sum(LLMInteractionLog.output_tokens), 0),
            func.coalesce(func.sum(LLMInteractionLog.cache_read_tokens), 0),
            func.coalesce(func.sum(LLMInteractionLog.cache_creation_tokens), 0),
            func.coalesce(func.sum(LLMInteractionLog.cost_usd), 0),
            func.count(LLMInteractionLog.id),
        )
        .filter(base_filter)
        .one()
    )

    input_tokens, output_tokens, cache_read, cache_creation, cost_usd, calls = row
    return {
        "calls": int(calls or 0),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_tokens": int(cache_read or 0),
        "cache_creation_tokens": int(cache_creation or 0),
        "cost_usd": float(cost_usd or 0),
        "cache_hit_rate": _cache_hit_rate(int(input_tokens or 0), int(cache_read or 0)),
    }


def _cost_group_by_agent(db: Session, base_filter) -> List[Dict[str, Any]]:
    rows = (
        db.query(
            LLMInteractionLog.agent_id,
            func.count(LLMInteractionLog.id).label("calls"),
            func.coalesce(func.sum(LLMInteractionLog.input_tokens), 0).label(
                "input_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.output_tokens), 0).label(
                "output_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cache_read_tokens), 0).label(
                "cache_read"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cache_creation_tokens), 0).label(
                "cache_creation"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cost_usd), 0).label("cost_usd"),
        )
        .filter(base_filter)
        .group_by(LLMInteractionLog.agent_id)
        .order_by(func.sum(LLMInteractionLog.cost_usd).desc().nullslast())
        .all()
    )

    return [
        {
            "agent_id": agent_id or "unknown",
            "calls": int(calls or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read or 0),
            "cache_creation_tokens": int(cache_creation or 0),
            "cost_usd": float(cost_usd or 0),
            "cache_hit_rate": _cache_hit_rate(
                int(input_tokens or 0), int(cache_read or 0)
            ),
        }
        for agent_id, calls, input_tokens, output_tokens, cache_read, cache_creation, cost_usd in rows
    ]


def _cost_group_by_model(db: Session, base_filter) -> List[Dict[str, Any]]:
    rows = (
        db.query(
            LLMInteractionLog.model,
            func.count(LLMInteractionLog.id).label("calls"),
            func.coalesce(func.sum(LLMInteractionLog.input_tokens), 0).label(
                "input_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.output_tokens), 0).label(
                "output_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cache_read_tokens), 0).label(
                "cache_read"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cache_creation_tokens), 0).label(
                "cache_creation"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cost_usd), 0).label("cost_usd"),
        )
        .filter(base_filter)
        .group_by(LLMInteractionLog.model)
        .order_by(func.sum(LLMInteractionLog.cost_usd).desc().nullslast())
        .all()
    )

    # #184 Phase 3: surface pricing_source per row so the dashboard can
    # badge "heuristic" / "unknown" models — those rows record cost from
    # tier-regex pricing (or $0 for unknown) and need to be visually
    # distinguishable from "exact" rows. Provider is inferred from the
    # model id since LLMInteractionLog doesn't carry provider_type.

    registry = get_registry()
    return [
        {
            "model": model or "unknown",
            "provider_type": infer_provider_type(model or ""),
            "pricing_source": registry.get_pricing_source(
                model or "", infer_provider_type(model or "")
            ),
            "calls": int(calls or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read or 0),
            "cache_creation_tokens": int(cache_creation or 0),
            "cost_usd": float(cost_usd or 0),
            "cache_hit_rate": _cache_hit_rate(
                int(input_tokens or 0), int(cache_read or 0)
            ),
        }
        for model, calls, input_tokens, output_tokens, cache_read, cache_creation, cost_usd in rows
    ]


def _cost_top_investigations(
    db: Session, base_filter, limit: int = 10
) -> List[Dict[str, Any]]:
    rows = (
        db.query(
            LLMInteractionLog.investigation_id,
            func.count(LLMInteractionLog.id).label("calls"),
            func.coalesce(func.sum(LLMInteractionLog.input_tokens), 0).label(
                "input_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.output_tokens), 0).label(
                "output_tokens"
            ),
            func.coalesce(func.sum(LLMInteractionLog.cost_usd), 0).label("cost_usd"),
        )
        .filter(and_(base_filter, LLMInteractionLog.investigation_id.isnot(None)))
        .group_by(LLMInteractionLog.investigation_id)
        .order_by(func.sum(LLMInteractionLog.cost_usd).desc().nullslast())
        .limit(limit)
        .all()
    )

    return [
        {
            "investigation_id": inv_id,
            "calls": int(calls or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cost_usd": float(cost_usd or 0),
        }
        for inv_id, calls, input_tokens, output_tokens, cost_usd in rows
    ]


async def get_cost_breakdown(db: Session, time_range: str) -> Dict[str, Any]:
    """LLM cost + token breakdown for the window named by ``time_range``.

    ``time_series`` comes from Bifrost and is ``None`` when it is unreachable,
    so the dashboard degrades to local aggregations rather than failing.
    """
    start_time, end_time = get_time_range(time_range)
    base_filter = and_(
        LLMInteractionLog.created_at >= start_time,
        LLMInteractionLog.created_at <= end_time,
    )
    return {
        "window": {
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "seconds": int((end_time - start_time).total_seconds()),
        },
        "totals": _cost_totals(db, base_filter),
        "by_agent": _cost_group_by_agent(db, base_filter),
        "by_model": _cost_group_by_model(db, base_filter),
        "top_investigations": _cost_top_investigations(db, base_filter),
        "time_series": _cost_time_series_from_bifrost(start_time, end_time),
    }
