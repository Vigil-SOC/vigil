"""Tests for ``services.bifrost_cost_client`` (#185).

The client wraps Bifrost's logging-plugin endpoints. Tests stub the HTTP
layer so they don't need a running Bifrost — what we're verifying is:

  * The right URL gets called for each method.
  * Filter dicts get flattened to comma-separated query params (Bifrost's
    convention) so callers can pass real Python lists.
  * Failures (timeouts, 5xx, network errors) return ``None`` instead of
    raising — the cost dashboard must degrade gracefully when Bifrost is
    unreachable.
  * The recalculate-cost path POSTs the filter body Bifrost expects.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


pytestmark = pytest.mark.unit


def _ok_response(payload):
    """Build a fake httpx.Response that returns ``payload``."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def _err_response(status: int, text: str = "boom"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


def _client_returning(method: str, response):
    """Build a context-manager mock client whose ``method`` returns ``response``."""
    client = MagicMock()
    setattr(client, method, MagicMock(return_value=response))
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, client


# ---------------------------------------------------------------------------
# histogram_cost — the main analytics path
# ---------------------------------------------------------------------------


def test_histogram_cost_hits_correct_url_and_returns_payload(monkeypatch):
    monkeypatch.setenv("BIFROST_URL", "http://bifrost-test:8080")
    payload = {
        "buckets": [
            {"timestamp": "2026-05-04T12:00:00Z", "total_cost": 1.23, "by_model": {}}
        ],
        "bucket_size_seconds": 3600,
        "models": [],
    }
    cm, client = _client_returning("get", _ok_response(payload))

    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import histogram_cost

        result = histogram_cost(
            start_time="2026-05-04T00:00:00Z",
            end_time="2026-05-05T00:00:00Z",
            providers=["openai", "anthropic"],
        )

    assert result == payload
    args, kwargs = client.get.call_args
    # Path
    assert args[0] == "http://bifrost-test:8080/api/logs/histogram/cost"
    # Filter list flattened to comma-separated string per Bifrost's convention
    params = kwargs["params"]
    assert params["providers"] == "openai,anthropic"
    assert params["start_time"] == "2026-05-04T00:00:00Z"
    assert params["end_time"] == "2026-05-05T00:00:00Z"


def test_histogram_cost_returns_none_on_5xx(monkeypatch):
    """The dashboard must degrade gracefully — Bifrost down means we
    fall back to local LLMInteractionLog aggregations, not 500 the user."""
    cm, _ = _client_returning("get", _err_response(503, "service down"))

    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import histogram_cost

        assert histogram_cost() is None


def test_histogram_cost_returns_none_on_network_error():
    """Connection refused (Bifrost not running) must not raise."""
    cm = MagicMock()
    client = MagicMock()
    client.get = MagicMock(side_effect=ConnectionError("nope"))
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import histogram_cost

        assert histogram_cost() is None


# ---------------------------------------------------------------------------
# stats / search / cost-by-provider — same wiring, smaller coverage
# ---------------------------------------------------------------------------


def test_stats_hits_correct_path():
    cm, client = _client_returning("get", _ok_response({"total_requests": 0}))
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import stats

        out = stats()
        assert out == {"total_requests": 0}
        assert client.get.call_args.args[0].endswith("/api/logs/stats")


def test_search_logs_clamps_limit():
    """Bifrost docs cap limit at 1000 — clamp client-side too."""
    cm, client = _client_returning("get", _ok_response({"logs": []}))
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import search_logs

        search_logs(limit=99999)
        assert client.get.call_args.kwargs["params"]["limit"] == 1000


def test_search_logs_negative_offset_clamped_to_zero():
    cm, client = _client_returning("get", _ok_response({"logs": []}))
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import search_logs

        search_logs(offset=-50)
        assert client.get.call_args.kwargs["params"]["offset"] == 0


# ---------------------------------------------------------------------------
# recalculate_cost — POST body shape + cap
# ---------------------------------------------------------------------------


def test_recalculate_cost_posts_filters_and_limit():
    cm, client = _client_returning(
        "post",
        _ok_response({"total_matched": 50, "updated": 50, "skipped": 0, "remaining": 0}),
    )
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import recalculate_cost

        out = recalculate_cost(
            filters={"missing_cost_only": True, "providers": ["anthropic"]},
            limit=200,
        )
    assert out == {"total_matched": 50, "updated": 50, "skipped": 0, "remaining": 0}
    body = client.post.call_args.kwargs["json"]
    assert body["limit"] == 200
    assert body["filters"]["missing_cost_only"] is True
    assert body["filters"]["providers"] == ["anthropic"]


def test_recalculate_cost_limit_clamped_to_1000():
    cm, client = _client_returning("post", _ok_response({"updated": 1000, "remaining": 0}))
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import recalculate_cost

        recalculate_cost(limit=99999)
    assert client.post.call_args.kwargs["json"]["limit"] == 1000


def test_recalculate_cost_returns_none_on_400():
    """The endpoint surfaces invalid filters as 400 — caller treats it
    as a soft failure (UI shows error message, doesn't crash)."""
    cm, _ = _client_returning("post", _err_response(400, "bad request"))
    with patch("services.bifrost_cost_client.httpx.Client", return_value=cm):
        from services.bifrost_cost_client import recalculate_cost

        assert recalculate_cost() is None
