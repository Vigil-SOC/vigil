"""Generate with AI keeps the API answering while the model writes (#3180).

The model assignment lookup reads the database and blocks. It is made to sleep
here; a concurrent ``GET /api/health`` must still come back at once, which only
holds if the lookup runs off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

pytestmark = pytest.mark.unit

BLOCK_S = 5.0

WORKFLOW = json.dumps({"name": "W", "description": "d", "phases": []})
AGENT = json.dumps({"name": "A", "description": "d", "system_prompt": "p"})


class _Storage:
    def probe_connection(self):
        pass

    def get_backend_info(self):
        return {"backend": "none", "database_available": False, "demo_mode": False}


@pytest.fixture
def app(monkeypatch, authenticate_app):
    from fastapi import FastAPI

    from core.agents.agent_ai_generator import AgentAIGenerator
    from core.llm import target
    from core.llm.router.router import LLMRouter, ProviderSpec
    from core.workflows import workflows_router
    from core.workflows.workflow_ai_generator import WorkflowAIGenerator
    from services.api.main import health_check
    from services.api.routers import custom_agents, custom_integrations

    reply = {"value": "", "blocked_at": None}

    provider = ProviderSpec(
        provider_id="ollama-local",
        provider_type="ollama",
        base_url="http://127.0.0.1:11434",
        api_key_ref=None,
        default_model="m",
        config={},
    )

    def slow_resolve(component):
        reply["blocked_at"] = time.monotonic()
        time.sleep(BLOCK_S)
        return provider, "m"

    async def dispatch(self, **kwargs):
        return {"content": reply["value"]}

    monkeypatch.setattr(target, "resolve_dispatch", slow_resolve)
    monkeypatch.setattr(target, "resolve_effort", lambda component: None)
    monkeypatch.setattr(LLMRouter, "dispatch", dispatch)
    monkeypatch.setattr(custom_integrations, "deny_unless", lambda *a, **k: None)

    # A fresh app with the real routers and health handler, so the shared
    # services.api.main app is left untouched for other tests.
    app = FastAPI()
    for module in (workflows_router, custom_agents, custom_integrations):
        app.include_router(module.router, prefix=module.ROUTER_META.prefix)
    app.add_api_route("/api/health", health_check, methods=["GET"])
    authenticate_app(app)
    app.state.health_storage = _Storage()
    app.state.workflow_ai = WorkflowAIGenerator()
    app.state.agent_ai = AgentAIGenerator()
    app.state.test_reply = reply
    return app


@pytest.mark.parametrize(
    ("path", "body", "reply"),
    [
        ("/api/workflows/generate", {"description": "triage phish"}, WORKFLOW),
        ("/api/agents/custom/generate", {"description": "triage phish"}, AGENT),
        (
            "/api/custom-integrations/generate",
            {"documentation": "GET /v1/alerts"},
            "I have some questions: which auth scheme?",
        ),
    ],
)
async def test_health_answers_while_a_draft_generates(app, path, body, reply):
    state = app.state.test_reply
    state.update(value=reply, blocked_at=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        generate = asyncio.create_task(client.post(path, json=body))
        # Until the assignment lookup starts blocking (or the request fails first).
        while state["blocked_at"] is None and not generate.done():
            await asyncio.sleep(0.01)
        assert state["blocked_at"] is not None, (await generate).text
        health = await client.get("/api/health")
        # Timed from when blocking began: a blocked loop stalls this poll too.
        elapsed = time.monotonic() - state["blocked_at"]

        response = await generate

    assert health.status_code == 200
    assert response.status_code == 200, response.text
    # A blocked loop waits out BLOCK_S; a fraction of it absorbs runner jitter.
    assert elapsed < BLOCK_S / 5, f"/api/health waited {elapsed:.2f}s behind {path}"
