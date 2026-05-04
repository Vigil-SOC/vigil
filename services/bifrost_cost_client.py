"""Read-side client for Bifrost's logging API (#185).

Bifrost is the authoritative cost source for every LLM call Vigil makes —
its logging plugin records exact cost against current pricing for every
upstream request, with built-in batch-recompute for retroactive repricing.
This module is the one place the backend talks to that read-side API,
mirroring the pattern set by ``services.bifrost_admin`` (module-level
functions, env-driven base URL, ``httpx.Client``, failures returned as
``None``/empty rather than raised).

What's wrapped (verified against docs.getbifrost.ai):

  * ``GET /api/logs/histogram/cost``   — time-bucketed cost + by_model
  * ``GET /api/logs/histogram/cost-by-provider``
  * ``GET /api/logs/histogram/token-usage``
  * ``GET /api/logs/stats``            — aggregate totals (cost, tokens,
                                          requests, latency, success rate)
  * ``GET /api/logs``                  — raw log search (limited use —
                                          custom-metadata filtering isn't
                                          a supported query param yet)
  * ``POST /api/logs/recalculate-cost``— batch recompute against current
                                          pricing (admin operation)

What's *not* wrapped here: provider config (``services.bifrost_admin``
already owns provider key/model writes) and governance/budget endpoints
(those land in PR C / #186 alongside the VK enforcement work).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0


def _bifrost_base_url() -> str:
    return os.getenv("BIFROST_URL", "http://localhost:8080").rstrip("/")


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def _filter_params(
    *,
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    virtual_key_ids: Optional[List[str]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    min_cost: Optional[float] = None,
    max_cost: Optional[float] = None,
    missing_cost_only: Optional[bool] = None,
    status: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the comma-separated query string Bifrost expects.

    All filter params are flattened to comma-separated strings (Bifrost's
    convention) so callers pass real Python lists. Falsy values are dropped
    so query strings stay tight.
    """
    params: Dict[str, Any] = {}
    if providers:
        params["providers"] = ",".join(providers)
    if models:
        params["models"] = ",".join(models)
    if virtual_key_ids:
        params["virtual_key_ids"] = ",".join(virtual_key_ids)
    if status:
        params["status"] = ",".join(status)
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if min_cost is not None:
        params["min_cost"] = min_cost
    if max_cost is not None:
        params["max_cost"] = max_cost
    if missing_cost_only is not None:
        params["missing_cost_only"] = "true" if missing_cost_only else "false"
    return params


# ---------------------------------------------------------------------------
# Histograms — time-bucketed analytics
# ---------------------------------------------------------------------------


def histogram_cost(
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    virtual_key_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Time-bucketed cost with per-model breakdown.

    Response shape (per Bifrost docs):

    .. code-block:: json

        {
          "buckets": [
            {"timestamp": "...", "total_cost": 125.50,
             "by_model": {"openai/gpt-4": 85.30, ...}}
          ],
          "bucket_size_seconds": 3600,
          "models": ["openai/gpt-4", ...]
        }

    Returns ``None`` on failure so the caller can fall back to local
    aggregation against ``LLMInteractionLog`` (the daemon's existing
    write path) without surfacing a 500 to the user.
    """
    return _get_json(
        "/api/logs/histogram/cost",
        params=_filter_params(
            start_time=start_time,
            end_time=end_time,
            providers=providers,
            models=models,
            virtual_key_ids=virtual_key_ids,
        ),
    )


def histogram_cost_by_provider(
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    providers: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Same shape as :func:`histogram_cost` but bucket payload groups by
    provider rather than model. Used by the dashboard's provider-mix view.
    """
    return _get_json(
        "/api/logs/histogram/cost-by-provider",
        params=_filter_params(
            start_time=start_time, end_time=end_time, providers=providers
        ),
    )


def histogram_token_usage(
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    models: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Time-bucketed token-usage histogram.

    Useful for separating prompt-cache savings (cache_read tokens are
    cheaper than fresh input) from raw input growth on the dashboard.
    """
    return _get_json(
        "/api/logs/histogram/token-usage",
        params=_filter_params(
            start_time=start_time, end_time=end_time, models=models
        ),
    )


# ---------------------------------------------------------------------------
# Aggregates / raw logs
# ---------------------------------------------------------------------------


def stats(
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    virtual_key_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate ``LogStats`` for the filtered window:

    - ``total_requests``, ``total_tokens``, ``total_cost``
    - ``average_latency``, ``success_rate``
    """
    return _get_json(
        "/api/logs/stats",
        params=_filter_params(
            start_time=start_time,
            end_time=end_time,
            providers=providers,
            models=models,
            virtual_key_ids=virtual_key_ids,
        ),
    )


def search_logs(
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    content_search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Optional[Dict[str, Any]]:
    """Raw log-search for ad-hoc inspection.

    Custom metadata filtering (e.g. matching the
    ``x-bf-lh-vigil-interaction-id`` header) is not currently exposed
    as a query param — callers that need correlation by Vigil's
    interaction_id must filter the response client-side. Watch the
    Bifrost docs for a future ``metadata_*`` query parameter.
    """
    params = _filter_params(
        start_time=start_time,
        end_time=end_time,
        providers=providers,
        models=models,
    )
    if content_search:
        params["content_search"] = content_search
    params["limit"] = max(1, min(int(limit), 1000))
    params["offset"] = max(0, int(offset))
    return _get_json("/api/logs", params=params)


# ---------------------------------------------------------------------------
# Recalculate cost — admin operation, the lever that fixes pricing rot
# ---------------------------------------------------------------------------


def recalculate_cost(
    *,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 200,
) -> Optional[Dict[str, Any]]:
    """Reprice historical logs against Bifrost's current pricing data.

    The default behavior (no filters, limit=200) re-costs the next 200
    rows that have ``missing_cost`` set. Pass ``filters`` to scope to a
    specific window/model after a known repricing event.

    Response (``RecalculateCostResponse``):

    - ``total_matched``: rows meeting the filter
    - ``updated``: rows successfully recalculated
    - ``skipped``: rows not processed (e.g. invalid token data)
    - ``remaining``: rows beyond the limit

    Hard-capped at 1000 per call by Bifrost; callers that need to
    reprice a larger backlog should loop on ``remaining``.
    """
    body: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if filters:
        body["filters"] = filters
    return _post_json("/api/logs/recalculate-cost", body=body)


# ---------------------------------------------------------------------------
# Internal HTTP helpers
# ---------------------------------------------------------------------------


def _get_json(path: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    url = f"{_bifrost_base_url()}{path}"
    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
            r = client.get(url, params=params or {})
            if r.status_code >= 400:
                logger.warning(
                    "bifrost_cost_client: GET %s returned %s: %s",
                    path,
                    r.status_code,
                    r.text[:200],
                )
                return None
            return r.json()
    except Exception as e:
        logger.warning("bifrost_cost_client: GET %s failed: %s", path, e)
        return None


def _post_json(path: str, *, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"{_bifrost_base_url()}{path}"
    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
            r = client.post(url, json=body)
            if r.status_code >= 400:
                logger.warning(
                    "bifrost_cost_client: POST %s returned %s: %s",
                    path,
                    r.status_code,
                    r.text[:200],
                )
                return None
            return r.json()
    except Exception as e:
        logger.warning("bifrost_cost_client: POST %s failed: %s", path, e)
        return None
