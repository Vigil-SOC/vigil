"""Generate with AI keeps the API answering while the model writes (#3180).

``ClaudeService`` construction (key lookup) and ``chat`` (SDK call + log write)
both block. Each is made to sleep here; a concurrent ``GET /api/health`` must
still come back at once, which only holds if both run off the event loop.
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
def app(monkeypatch):
    from fastapi import FastAPI

    from core.agents.agent_ai_generator import AgentAIGenerator
    from core.llm.harness.claude import ClaudeService
    from core.workflows import workflows_router
    from core.workflows.workflow_ai_generator import WorkflowAIGenerator
    from services.api.main import health_check
    from services.api.middleware.auth import get_current_active_user
    from services.api.routers import custom_agents, custom_integrations

    reply = {"value": "", "blocked_at": None}

    def slow_load(self):
        reply["blocked_at"] = time.monotonic()
        time.sleep(BLOCK_S)
        self.api_key, self.client = "k", object()
        return True

    def slow_chat(self, *args, **kwargs):
        time.sleep(BLOCK_S)
        return reply["value"]

    monkeypatch.setattr(ClaudeService, "_load_api_key", slow_load)
    monkeypatch.setattr(ClaudeService, "chat", slow_chat)
    monkeypatch.setattr(
        custom_integrations, "require_integrations_admin", lambda u: None
    )

    # A fresh app with the real routers and health handler, so the shared
    # services.api.main app is left untouched for other tests.
    app = FastAPI()
    for module in (workflows_router, custom_agents, custom_integrations):
        app.include_router(module.router, prefix=module.ROUTER_META.prefix)
    app.add_api_route("/api/health", health_check, methods=["GET"])
    app.dependency_overrides[get_current_active_user] = lambda: object()
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
        # Until the key lookup starts blocking (or the request fails first).
        while state["blocked_at"] is None and not generate.done():
            await asyncio.sleep(0.01)
        assert state["blocked_at"] is not None, (await generate).text
        health = await client.get("/api/health")
        # Timed from when blocking began: a blocked loop stalls this poll too.
        elapsed = time.monotonic() - state["blocked_at"]

        response = await generate

    assert health.status_code == 200
    assert response.status_code == 200, response.text
    assert elapsed < 0.1, f"/api/health waited {elapsed:.2f}s behind {path}"
