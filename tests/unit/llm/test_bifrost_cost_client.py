"""Tests for ``core.llm.bifrost.costs`` (#185).

The client wraps Bifrost's logging-plugin endpoints. Tests stub the HTTP
layer so they don't need a running Bifrost — what we're verifying is:

  * The right URL gets called for each method.
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

REPO = Path(__file__).resolve().parent.parent.parent.parent
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
# recalculate_cost — POST body shape + cap
# ---------------------------------------------------------------------------


def test_recalculate_cost_posts_filters_and_limit():
    cm, client = _client_returning(
        "post",
        _ok_response(
            {"total_matched": 50, "updated": 50, "skipped": 0, "remaining": 0}
        ),
    )
    with patch("core.llm.bifrost.costs.httpx.Client", return_value=cm):
        from core.llm.bifrost.costs import recalculate_cost

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
    cm, client = _client_returning(
        "post", _ok_response({"updated": 1000, "remaining": 0})
    )
    with patch("core.llm.bifrost.costs.httpx.Client", return_value=cm):
        from core.llm.bifrost.costs import recalculate_cost

        recalculate_cost(limit=99999)
    assert client.post.call_args.kwargs["json"]["limit"] == 1000


def test_recalculate_cost_returns_none_on_400():
    """The endpoint surfaces invalid filters as 400 — caller treats it
    as a soft failure (UI shows error message, doesn't crash)."""
    cm, _ = _client_returning("post", _err_response(400, "bad request"))
    with patch("core.llm.bifrost.costs.httpx.Client", return_value=cm):
        from core.llm.bifrost.costs import recalculate_cost

        assert recalculate_cost() is None
