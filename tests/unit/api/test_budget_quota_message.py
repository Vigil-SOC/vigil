"""The spending limit card's "no key" message names a page that exists."""

from __future__ import annotations

import asyncio

import pytest

from services.api.routers import budgets

pytestmark = pytest.mark.unit


def test_quota_without_a_virtual_key_points_at_settings_ai_models(monkeypatch):
    monkeypatch.setattr(budgets, "get_active_vk", lambda: None)

    body = asyncio.run(budgets.get_budget_quota())

    assert body["configured"] is False
    assert "Settings › AI models" in body["message"]
    assert "LLM Providers" not in body["message"]
