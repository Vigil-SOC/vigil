"""A provider added a moment ago is priced on the agent's first question (#2290).

Only the API worker that took the key write syncs at once; the others hold no
rates for the new provider until their next catalogue refresh, minutes later. A
hunt started in that window parked on its first iteration as unpriced.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents import internal_auth
from core.llm.bifrost import admin
from core.llm.cost import pricing_router
from core.llm.providers import registry
from core.llm.providers.discovery import ModelMeta

pytestmark = pytest.mark.unit

HAIKU = "anthropic/claude-haiku-5.5"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "secret")
    monkeypatch.setattr(pricing_router, "_last_reread", {})
    registry.clear_live_meta()
    app = FastAPI()
    app.include_router(pricing_router.router, prefix="/internal/pricing")
    yield TestClient(app)
    registry.clear_live_meta()


def _rates(client):
    return client.get(
        "/internal/pricing/rates",
        params={"model_id": HAIKU, "provider_type": "openrouter"},
        headers={"Authorization": "Bearer secret"},
    ).json()


def test_an_unknown_rate_reads_the_new_providers_sheet_once(client, monkeypatch):
    reads = []

    async def sheet(types):
        reads.append(list(types))
        meta = [
            ModelMeta(
                id=HAIKU,
                display_name=HAIKU,
                input_cost_per_token=1e-06,
                output_cost_per_token=5e-06,
                cache_read_cost_per_token=1e-07,
                cache_write_cost_per_token=1.25e-06,
            )
        ]
        registry.record_live_meta("openrouter", meta, rates_only=True)
        return {"openrouter": meta}

    monkeypatch.setattr(admin, "record_gateway_rates", sheet)

    body = _rates(client)

    assert body["source"] == "exact"
    assert (body["input"], body["output"]) == (1e-06, 5e-06)
    assert reads == [["openrouter"]]
    _rates(client)
    assert reads == [["openrouter"]]  # priced now, so no second read


def test_a_rate_the_gateway_cannot_give_is_not_reread_on_every_call(
    client, monkeypatch
):
    reads = []

    async def empty_sheet(types):
        reads.append(list(types))
        return {"openrouter": None}

    monkeypatch.setattr(admin, "record_gateway_rates", empty_sheet)

    assert _rates(client)["source"] == "unknown"
    assert _rates(client)["source"] == "unknown"
    assert len(reads) == 1
