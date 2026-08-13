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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.llm.providers.registry import get_registry, infer_provider_type
from core.reporting.ai_insights_service import AIInsightsService
from core.reporting.analytics_service import (
    calculate_metrics,
    collect_insights_inputs,
    get_affected_entities,
    get_attack_time_heatmap,
    get_cost_breakdown,
    get_mitre_technique_distribution,
    get_response_time_trend,
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
    try:
        start_time, end_time = get_time_range(time_range)

        # Get previous period for comparison
        period_duration = end_time - start_time
        prev_start = start_time - period_duration

        # Calculate key metrics
        metrics = await calculate_metrics(
            db, start_time, end_time, prev_start, start_time
        )

        # Get time series data
        time_series = await get_time_series_data(db, start_time, end_time, time_range)

        # Get severity distribution
        severity_dist = await get_severity_distribution(db, start_time, end_time)

        # Get top sources
        top_sources = await get_top_alert_sources(db, start_time, end_time)

        # Get response time trend
        response_time_data = await get_response_time_trend(
            db, start_time, end_time, time_range
        )

        # Get affected entities/devices
        affected_entities = await get_affected_entities(db, start_time, end_time)

        # Get attack time heatmap
        attack_heatmap = await get_attack_time_heatmap(db, start_time, end_time)

        # Get MITRE technique distribution
        mitre_techniques = await get_mitre_technique_distribution(
            db, start_time, end_time
        )

        # NOTE: AI insights are intentionally NOT generated here — they are
        # served from an in-memory cache via GET /analytics/insights so this
        # endpoint returns fast and never blocks on the Claude API.
        return {
            "metrics": metrics,
            "timeSeriesData": time_series,
            "severityDistribution": severity_dist,
            "topSources": top_sources,
            "responseTimeData": response_time_data,
            "affectedEntities": affected_entities,
            "attackHeatmap": attack_heatmap,
            "mitreTechniques": mitre_techniques,
        }

    except Exception as e:
        logger.error(f"Error generating analytics: {str(e)}")
        raise


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
                    db=db,
                    metrics=metrics,
                    time_series=time_series,
                    time_range=time_range,
                )
            )
            cached["generating"] = True
        except Exception as e:
            logger.warning(f"Could not schedule insights regeneration: {e}")

    return cached


@router.post("/analytics/insights/refresh")
async def refresh_analytics_insights(
    time_range: str = Query("7d", pattern="^(24h|7d|30d|all)$"),
    *,
    db: UnitOfWorkSession,
) -> Dict[str, Any]:
    """Force a background regeneration of insights for the given time_range.

    Returns immediately with ``{"status": "refreshing"}`` or
    ``{"status": "already_generating"}`` if one is already in flight. Clients
    should then poll GET /analytics/insights until ``generated_at`` changes.
    """
    cached = ai_insights_service.get_cached_insights(time_range)
    if cached["generating"]:
        return {"status": "already_generating", "generated_at": cached["generated_at"]}

    try:
        metrics, time_series = await collect_insights_inputs(db, time_range)
    except Exception as e:
        logger.error(f"Could not collect inputs for insights refresh: {e}")
        return {"status": "error", "message": str(e)}

    asyncio.create_task(
        ai_insights_service.trigger_regeneration(
            db=db,
            metrics=metrics,
            time_series=time_series,
            time_range=time_range,
        )
    )
    return {"status": "refreshing", "generated_at": cached["generated_at"]}


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


# ---------------------------------------------------------------------------
# Pre-call cost estimation (#184 Phase 2)
# ---------------------------------------------------------------------------


class EstimateCostRequest(BaseModel):
    """Body for POST /analytics/estimate-cost.

    Mirrors the shape of an LLM call so the same payload a caller is
    about to send can be passed straight in for an estimate. ``messages``
    matches Anthropic / OpenAI message format — list of role+content
    dicts; multimodal blocks are tolerated but token-counted as text-only
    in the heuristic path.
    """

    provider_type: Optional[str] = Field(
        default=None,
        description=(
            "anthropic | openai | ollama. Advisory only — the endpoint "
            "resolves the real provider from model_id (registry, then name "
            "heuristic) and uses this value only as a last-resort fallback."
        ),
    )
    model_id: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    max_tokens: int = Field(default=4096, ge=1, le=200_000)


@router.post("/analytics/estimate-cost")
async def estimate_cost_endpoint(payload: EstimateCostRequest) -> Dict[str, Any]:
    """Return a USD low/high band for a hypothetical LLM call.

    The chat composer and daemon planner call this before submitting a
    real request so they can show the user a cost preview or gate the
    call against a budget. ``low_usd`` assumes zero output tokens (e.g.
    an immediate tool_use stop); ``high_usd`` assumes the call writes
    out ``max_tokens`` of output. Real-world cost lands in between, and
    is typically much closer to ``low_usd`` for cache-friendly workloads.
    """
    from core.llm.cost.estimator import estimate_cost

    # The chat composer can't reliably know which provider serves a given
    # model (the bare model id it picked routes to the active default on the
    # send path), so it sends a placeholder provider_type. Resolve the real
    # provider here so the estimate matches what the send path will actually
    # do — otherwise an Ollama model gets priced as Anthropic and logs a
    # spurious "No catalog entry for anthropic/<model>" warning.
    provider_type = payload.provider_type
    resolved: Optional[str] = None
    try:
        for m in await get_registry().list_available_models():
            if m.model_id == payload.model_id:
                resolved = m.provider_type
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("estimate-cost: registry lookup failed: %s", exc)
    if resolved is None:
        inferred = infer_provider_type(payload.model_id)
        if inferred != "unknown":
            resolved = inferred
    if resolved is not None:
        provider_type = resolved
    if not provider_type:
        # No registry match, heuristic said "unknown", and no caller hint.
        # Hand estimate_cost an explicit unknown so it returns $0 with
        # pricing_source="unknown" instead of tripping the required-field.
        provider_type = "unknown"

    estimate = await estimate_cost(
        provider_type=provider_type,
        model_id=payload.model_id,
        messages=payload.messages,
        system_prompt=payload.system_prompt,
        tools=payload.tools,
        max_tokens=payload.max_tokens,
    )
    return estimate.to_dict()


# ---------------------------------------------------------------------------
# Recalculate cost — admin operation that fixes pricing rot (#185)
# ---------------------------------------------------------------------------


class RecalculateCostRequest(BaseModel):
    """Body for POST /analytics/recalculate-cost.

    Optional filters scope which Bifrost log rows get re-costed. The
    common case (no filters) only touches rows where Bifrost recorded
    ``missing_cost`` — i.e. calls that happened before pricing data
    was available. After a known repricing event, scope by model or
    time window to reprice only the affected rows.
    """

    providers: Optional[List[str]] = None
    models: Optional[List[str]] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    missing_cost_only: Optional[bool] = True
    limit: int = Field(default=200, ge=1, le=1000)


@router.post("/analytics/recalculate-cost")
async def recalculate_cost_endpoint(
    payload: Optional[RecalculateCostRequest] = None,
) -> Dict[str, Any]:
    """Trigger Bifrost's batch cost-recompute against current pricing.

    Admin operation. When Anthropic or OpenAI publishes new pricing,
    Bifrost's catalog updates automatically — but historical log rows
    keep the cost they were billed at the time. This endpoint asks
    Bifrost to re-cost a batch of rows so the time-series and
    histograms reflect the new pricing without a redeploy.

    NOTE: this is a *cumulative* operation. Bifrost caps each call at
    1000 rows; the response's ``remaining`` field tells the caller how
    many rows still need processing. The UI button loops until
    ``remaining == 0`` (or fails fast on a 5xx).
    """
    from core.llm.bifrost.costs import recalculate_cost

    p = payload or RecalculateCostRequest()
    filters: Dict[str, Any] = {}
    if p.providers:
        filters["providers"] = p.providers
    if p.models:
        filters["models"] = p.models
    if p.start_time:
        filters["start_time"] = p.start_time
    if p.end_time:
        filters["end_time"] = p.end_time
    if p.missing_cost_only is not None:
        filters["missing_cost_only"] = p.missing_cost_only

    result = recalculate_cost(filters=filters or None, limit=p.limit)
    if result is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "Bifrost recalculate-cost call failed — check that Bifrost "
                "is reachable and the logging plugin is enabled with a "
                "persistence backend (see infra/docker/bifrost/README.md)."
            ),
        )
    return result
