"""
AI Insights Service - Generate intelligent insights using Claude

This service analyzes SOC metrics and patterns to generate:
- Anomaly detection
- Trend analysis and forecasting
- Actionable recommendations
- Performance optimization suggestions
"""

import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import asyncio
from sqlalchemy.orm import Session

from backend.secrets_manager import get_secret
from services.llm_clients import create_anthropic_client

logger = logging.getLogger(__name__)

# Cache TTL: insights older than this are considered stale and trigger a
# background regeneration on the next read.
CACHE_TTL_SECONDS = 600  # 10 minutes


class AIInsightsService:
    """Service for generating AI-powered insights from SOC data."""

    def __init__(self):
        """Initialize the AI insights service with Claude API client."""
        api_key = (get_secret("ANTHROPIC_API_KEY") or
                   get_secret("CLAUDE_API_KEY") or
                   get_secret("anthropic_api_key"))
        if not api_key:
            logger.warning("No Anthropic API key found - AI insights will use fallback mode")
            self.client = None
        else:
            self.client = create_anthropic_client(api_key)
        self.model = "claude-sonnet-4-20250514"  # Claude 4.5 Sonnet

        # In-memory cache of insights keyed by time_range.
        # Each entry: {"insights": List[Dict], "generated_at": datetime, "generating": bool}
        # A module-level singleton of this service is instantiated in
        # backend/api/analytics.py, so this dict is shared across requests
        # within the single backend process.
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = asyncio.Lock()

    def get_cached_insights(self, time_range: str) -> Dict[str, Any]:
        """Return the cached insights payload for a given time_range.

        Always returns a dict (never raises / never None) with shape::

            {
                "insights": List[Dict],
                "generated_at": Optional[str ISO],
                "is_stale": bool,
                "generating": bool,
            }
        """
        entry = self._cache.get(time_range)
        if not entry:
            return {
                "insights": [],
                "generated_at": None,
                "is_stale": True,
                "generating": False,
            }

        generated_at: datetime = entry["generated_at"]
        age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
        is_stale = age_seconds > CACHE_TTL_SECONDS

        return {
            "insights": entry["insights"],
            "generated_at": generated_at.isoformat(),
            "is_stale": is_stale,
            "generating": entry.get("generating", False),
        }

    async def trigger_regeneration(
        self,
        db: Session,
        metrics: Dict[str, Any],
        time_series: List[Dict[str, Any]],
        time_range: str,
    ) -> bool:
        """Regenerate insights in the background.

        Guards against concurrent regenerations for the same time_range via
        the ``generating`` flag. This method is designed to be scheduled with
        ``asyncio.create_task(...)`` — it will not raise, and returns True if
        it actually ran a regeneration, False if one was already in flight.
        """
        async with self._cache_lock:
            existing = self._cache.get(time_range)
            if existing and existing.get("generating"):
                return False
            # Mark as generating so concurrent calls short-circuit.
            if existing:
                existing["generating"] = True
            else:
                self._cache[time_range] = {
                    "insights": [],
                    "generated_at": datetime.now(timezone.utc),
                    "generating": True,
                }

        try:
            insights = await self.generate_insights(
                db=db,
                metrics=metrics,
                time_series=time_series,
                time_range=time_range,
            )
            async with self._cache_lock:
                self._cache[time_range] = {
                    "insights": insights,
                    "generated_at": datetime.now(timezone.utc),
                    "generating": False,
                }
            return True
        except Exception as e:
            logger.error(f"Background insights regeneration failed: {e}")
            async with self._cache_lock:
                if time_range in self._cache:
                    self._cache[time_range]["generating"] = False
            return False
    
    async def generate_insights(
        self,
        db: Session,
        metrics: Dict[str, Any],
        time_series: List[Dict[str, Any]],
        time_range: str
    ) -> List[Dict[str, Any]]:
        """
        Generate AI-powered insights from analytics data.
        
        Args:
            db: Database session
            metrics: Key SOC metrics
            time_series: Time series data for trends
            time_range: Time range for the analysis
            
        Returns:
            List of insights with recommendations, warnings, and anomalies
        """
        try:
            # If no API client available, use fallback insights
            if not self.client:
                logger.info("No Claude API client - using fallback insights")
                return self._get_fallback_insights(metrics)
            
            # Prepare context for Claude
            context = self._prepare_context(metrics, time_series, time_range)
            
            # Generate insights using Claude
            insights_text = await self._call_claude(context)
            
            # Parse insights from Claude's response
            insights = self._parse_insights(insights_text)
            
            return insights
        
        except Exception as e:
            logger.error(f"Error generating AI insights: {str(e)}")
            # Return fallback insights
            return self._get_fallback_insights(metrics)
    
    def _prepare_context(
        self,
        metrics: Dict[str, Any],
        time_series: List[Dict[str, Any]],
        time_range: str
    ) -> str:
        """Prepare context for Claude to analyze."""
        
        context = f"""You are an expert Security Operations Center (SOC) analyst AI assistant. Analyze the following SOC metrics and provide actionable insights.

Time Range: {time_range}

Current Metrics:
- Total Findings: {metrics['totalFindings']} ({metrics['findingsChange']:+.1f}% vs previous period)
- Active Cases: {metrics['totalCases']} ({metrics['casesChange']:+.1f}% vs previous period)
- Average Response Time: {metrics['avgResponseTime']} minutes ({metrics['responseTimeChange']:+.1f}% vs previous period)
- False Positive Rate: {metrics['falsePositiveRate']}% ({metrics['falsePositiveChange']:+.1f}% vs previous period)

Time Series Data (Recent samples):
{json.dumps(time_series[-5:], indent=2)}

Based on this data, provide 3-5 insights in the following categories:
1. **Anomalies**: Unusual patterns or spikes that need immediate attention
2. **Recommendations**: Actionable suggestions for improving SOC efficiency
3. **Trends**: Notable trends that indicate positive or negative directions
4. **Warnings**: Potential issues that may need proactive attention

For each insight, provide:
- type: One of 'anomaly', 'recommendation', 'warning', or 'info'
- title: A concise title (max 60 characters)
- description: A clear, actionable description (max 200 characters)
- confidence: Your confidence level (0.0 to 1.0)
- actionable: Whether this requires action (true/false)

Format your response as a JSON array of insights. Example:
[
  {{
    "type": "recommendation",
    "title": "Optimize alert tuning",
    "description": "False positive rate increased by 5%. Review detection rules for tuning opportunities.",
    "confidence": 0.85,
    "actionable": true
  }}
]

Provide ONLY the JSON array, no other text."""

        return context
    
    async def _call_claude(self, context: str) -> str:
        """Call Claude API via the LLM queue for global rate limiting."""
        try:
            from services.llm_gateway import get_llm_gateway
            gateway = await get_llm_gateway()
            result = await gateway.submit_insights(
                prompt=context,
                model=self.model,
                max_tokens=2000,
                temperature=0.3,
            )
            if result is None:
                raise ValueError("Empty response from LLM queue")
            if isinstance(result, dict):
                return result.get("content", "")
            return str(result)
        except ImportError:
            if not self.client:
                raise ValueError("Claude API client not initialized")
            loop = asyncio.get_event_loop()
            message = await loop.run_in_executor(
                None,
                lambda: self.client.messages.create(
                    model=self.model,
                    max_tokens=2000,
                    temperature=0.3,
                    messages=[{"role": "user", "content": context}]
                )
            )
            return message.content[0].text
        except Exception as e:
            logger.error(f"Error calling Claude API: {str(e)}")
            raise
    
    def _parse_insights(self, insights_text: str) -> List[Dict[str, Any]]:
        """Parse insights from Claude's JSON response."""
        try:
            # Extract JSON from response (in case there's extra text)
            start = insights_text.find('[')
            end = insights_text.rfind(']') + 1
            
            if start == -1 or end == 0:
                logger.warning("No JSON array found in Claude response")
                return []
            
            json_text = insights_text[start:end]
            insights_raw = json.loads(json_text)
            
            # Add timestamps and IDs
            insights = []
            for i, insight in enumerate(insights_raw):
                insights.append({
                    "id": f"insight-{datetime.utcnow().timestamp()}-{i}",
                    "type": insight.get("type", "info"),
                    "title": insight.get("title", "Insight"),
                    "description": insight.get("description", ""),
                    "confidence": insight.get("confidence", 0.7),
                    "actionable": insight.get("actionable", False),
                    "timestamp": datetime.utcnow().isoformat(),
                })
            
            return insights
        
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing insights JSON: {str(e)}")
            logger.debug(f"Raw response: {insights_text}")
            return []
        except Exception as e:
            logger.error(f"Error processing insights: {str(e)}")
            return []
    
    def _get_fallback_insights(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate fallback insights when AI service is unavailable."""
        insights = []
        timestamp = datetime.utcnow().isoformat()
        
        # Findings trend insight
        if abs(metrics['findingsChange']) > 20:
            insights.append({
                "id": f"fallback-findings-{datetime.utcnow().timestamp()}",
                "type": "warning" if metrics['findingsChange'] > 0 else "info",
                "title": f"Findings {'increased' if metrics['findingsChange'] > 0 else 'decreased'} significantly",
                "description": f"Findings changed by {metrics['findingsChange']:+.1f}% compared to previous period.",
                "confidence": 0.9,
                "actionable": metrics['findingsChange'] > 0,
                "timestamp": timestamp,
            })
        
        # Response time insight
        if metrics['avgResponseTime'] > 60:
            insights.append({
                "id": f"fallback-response-{datetime.utcnow().timestamp()}",
                "type": "warning",
                "title": "Response time exceeds target",
                "description": f"Average response time is {metrics['avgResponseTime']} minutes. Consider workload optimization.",
                "confidence": 0.85,
                "actionable": True,
                "timestamp": timestamp,
            })
        
        # False positive insight
        if metrics['falsePositiveRate'] > 30:
            insights.append({
                "id": f"fallback-fp-{datetime.utcnow().timestamp()}",
                "type": "recommendation",
                "title": "High false positive rate detected",
                "description": f"False positive rate is {metrics['falsePositiveRate']}%. Review detection rules for tuning.",
                "confidence": 0.9,
                "actionable": True,
                "timestamp": timestamp,
            })
        
        # Positive performance insight
        if metrics['responseTimeChange'] < -10:
            insights.append({
                "id": f"fallback-performance-{datetime.utcnow().timestamp()}",
                "type": "info",
                "title": "Response time improvement",
                "description": f"Response time improved by {abs(metrics['responseTimeChange']):.1f}%. Great work!",
                "confidence": 0.95,
                "actionable": False,
                "timestamp": timestamp,
            })
        
        return insights if insights else [{
            "id": f"fallback-default-{datetime.utcnow().timestamp()}",
            "type": "info",
            "title": "SOC operations stable",
            "description": "All metrics are within normal ranges. Continue monitoring for changes.",
            "confidence": 0.8,
            "actionable": False,
            "timestamp": timestamp,
        }]
    
    async def analyze_anomalies(
        self,
        db: Session,
        entity_type: str,
        entity_id: str,
        context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze a specific entity for anomalies using Claude.
        
        Args:
            db: Database session
            entity_type: Type of entity (finding, case, event)
            entity_id: ID of the entity
            context: Additional context for analysis
            
        Returns:
            Anomaly analysis result or None if no anomalies detected
        """
        try:
            prompt = f"""Analyze this {entity_type} for anomalies:

Entity ID: {entity_id}
Context: {json.dumps(context, indent=2)}

Determine if this represents an anomaly compared to typical patterns. Consider:
- Unusual timing or frequency
- Atypical severity or impact
- Rare entity combinations
- Deviation from baseline behavior

Respond with JSON:
{{
  "is_anomaly": true/false,
  "confidence": 0.0-1.0,
  "reason": "Brief explanation",
  "severity": "low|medium|high|critical"
}}"""

            from services.llm_gateway import get_llm_gateway
            gateway = await get_llm_gateway()
            raw = await gateway.submit_insights(
                prompt=prompt,
                model=self.model,
                max_tokens=500,
                temperature=0.2,
            )
            response_text = raw.get("content", "") if isinstance(raw, dict) else str(raw)
            
            # Parse JSON response
            start = response_text.find('{')
            end = response_text.rfind('}') + 1
            
            if start != -1 and end > 0:
                result = json.loads(response_text[start:end])
                return result if result.get('is_anomaly') else None
            
            return None
        
        except Exception as e:
            logger.error(f"Error analyzing anomaly: {str(e)}")
            return None
    
    async def forecast_trends(
        self,
        time_series: List[Dict[str, Any]],
        metric: str,
        periods_ahead: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Forecast future trends using Claude's analytical capabilities.
        
        Args:
            time_series: Historical time series data
            metric: Metric to forecast
            periods_ahead: Number of periods to forecast
            
        Returns:
            List of forecasted values with confidence intervals
        """
        try:
            prompt = f"""Based on this historical {metric} data, provide a simple forecast for the next {periods_ahead} periods:

Historical Data:
{json.dumps(time_series, indent=2)}

Respond with JSON array of forecasts:
[
  {{"period": 1, "value": <number>, "confidence": 0.0-1.0}},
  ...
]"""

            from services.llm_gateway import get_llm_gateway
            gateway = await get_llm_gateway()
            raw = await gateway.submit_insights(
                prompt=prompt,
                model=self.model,
                max_tokens=1000,
                temperature=0.3,
            )
            response_text = raw.get("content", "") if isinstance(raw, dict) else str(raw)
            
            # Parse JSON response
            start = response_text.find('[')
            end = response_text.rfind(']') + 1
            
            if start != -1 and end > 0:
                forecast = json.loads(response_text[start:end])
                return forecast
            
            return []
        
        except Exception as e:
            logger.error(f"Error forecasting trends: {str(e)}")
            return []

