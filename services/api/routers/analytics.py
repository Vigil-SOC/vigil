"""
Analytics API - Provides SOC metrics and AI-driven insights

This module exposes analytics data including:
- Key SOC metrics (findings, cases, response times)
- Time series data for trends
- Severity distributions
- AI-powered insights and recommendations
"""

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Query

from core.reporting.ai_insights_service import AIInsightsService
from core.reporting.analytics_service import (
    calculate_metrics,
    collect_insights_inputs,
    get_affected_entities,
    get_attack_time_heatmap,
    get_cost_breakdown,
    get_mitre_technique_distribution,
    get_severity_distribution,
    get_time_series_data,
    get_top_alert_sources,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.threat_intel.mitre_lookup import get_time_range

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["analytics"],
    auth=Auth.REQUIRED,
)
ai_insights_service = AIInsightsService()


@router.get("/analytics")
async def get_analytics(
    time_range: str = Query("7d", pattern="^(24h|7d|30d|all)$"),
    *,
    db: UnitOfWorkSession,
) -> Dict[str, Any]:
    """
    Get comprehensive analytics data for the specified time range.

    Args:
        time_range: Time range for analytics ('24h', '7d', '30d')
        db: Database session

    Returns:
        Dictionary containing metrics, time series data, distributions, and AI insights
    """
    start_time, end_time = get_time_range(time_range)

    # Get previous period for comparison
    period_duration = end_time - start_time
    prev_start = start_time - period_duration

    # Calculate key metrics
    metrics = await calculate_metrics(db, start_time, end_time, prev_start, start_time)

    # Get time series data
    time_series = await get_time_series_data(db, start_time, end_time, time_range)

    # Get severity distribution
    severity_dist = await get_severity_distribution(db, start_time, end_time)

    # Get top sources
    top_sources = await get_top_alert_sources(db, start_time, end_time)

    # Get affected entities/devices
    affected_entities = await get_affected_entities(db, start_time, end_time)

    # Get attack time heatmap
    attack_heatmap = await get_attack_time_heatmap(db, start_time, end_time)

    # Get MITRE technique distribution
    mitre_techniques = await get_mitre_technique_distribution(db, start_time, end_time)

    # NOTE: AI insights are intentionally NOT generated here — they are
    # served from an in-memory cache via GET /analytics/insights so this
    # endpoint returns fast and never blocks on the Claude API.
    return {
        "metrics": metrics,
        "timeSeriesData": time_series,
        "severityDistribution": severity_dist,
        "topSources": top_sources,
        "affectedEntities": affected_entities,
        "attackHeatmap": attack_heatmap,
        "mitreTechniques": mitre_techniques,
    }


@router.get("/analytics/insights")
async def get_analytics_insights(
    time_range: str = Query("7d", pattern="^(24h|7d|30d|all)$"),
    *,
    db: UnitOfWorkSession,
) -> Dict[str, Any]:
    """Return cached AI insights for the given time_range.

    Always returns immediately. If the cache is empty or stale, kicks off a
    background regeneration so the next poll will see fresh data. Callers
    should display ``insights`` as-is and show a staleness indicator when
    ``is_stale`` is true or ``generated_at`` is null.
    """
    cached = ai_insights_service.get_cached_insights(time_range)

    should_refresh = (
        cached["generated_at"] is None or cached["is_stale"]
    ) and not cached["generating"]

    if should_refresh:
        try:
            metrics, time_series = await collect_insights_inputs(db, time_range)
            asyncio.create_task(
                ai_insights_service.trigger_regeneration(
                    metrics=metrics,
                    time_series=time_series,
                    time_range=time_range,
                )
            )
            cached["generating"] = True
        except Exception as e:
            logger.warning(f"Could not schedule insights regeneration: {e}")

    return cached


# ---------------------------------------------------------------------------
# LLM cost analytics (GH #84 — Phase 1 Measurement)
# ---------------------------------------------------------------------------


@router.get("/analytics/cost")
async def get_cost_analytics(
    time_range: str = Query("7d", pattern="^(24h|7d|30d|all)$"),
    *,
    db: UnitOfWorkSession,
) -> Dict[str, Any]:
    """Return LLM cost + token breakdown for the given window.

    Groups `LLMInteractionLog` rows by agent, model, and investigation.
    Cache hit rate is cached-input / total-input across the window; it
    should read 0 until prompt caching ships in GH #84 PR-C — this
    endpoint is the baseline dashboard that will surface the jump.

    The ``time_series`` block is sourced from Bifrost's
    ``/api/logs/histogram/cost`` (#185), giving us authoritative cost
    actuals against current pricing. If Bifrost is unavailable the
    block is omitted but the local aggregations still return — the UI
    degrades gracefully from "actuals + trend" to "actuals only".
    """
    return await get_cost_breakdown(db, time_range)
