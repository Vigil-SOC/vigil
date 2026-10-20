"""A base URL saved through the console's endpoint is what a request is routed to.

Skipped unless BIFROST_URL points at a live Bifrost. The upstream is a stub
OpenAI-compatible server on a private address: the request must land there,
not on the stock OpenAI host, which is the regression gap #586 left open.

The gateway has to be able to reach the stub. With Bifrost in Docker that is
``host.docker.internal`` (the default); set BIFROST_STUB_HOST otherwise.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import bifrost_config as proxy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("BIFROST_URL"),
        reason="BIFROST_URL not set — Bifrost gateway not running",
    ),
]


class _Stub(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 0), _Handler)
        self.paths: list[str] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.server.paths.append(self.path)
        body = json.dumps(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": 1,
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "from-the-stub"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _chat(bifrost: str) -> httpx.Response:
    return httpx.post(
        f"{bifrost}/v1/chat/completions",
        json={"model": "openai/chain-test", "messages": [{"role": "user", "content": "hi"}]},
        timeout=30,
    )


def test_saved_base_url_routes_the_request_to_the_stub(monkeypatch):
    bifrost = os.environ["BIFROST_URL"].rstrip("/")
    monkeypatch.setattr(proxy, "get_settings", lambda: SimpleNamespace(bifrost_url=bifrost))
    monkeypatch.setattr(proxy, "_require_settings_admin", lambda user: None)
    app = FastAPI()
    app.include_router(proxy.router, prefix="/api/bifrost")
    app.dependency_overrides[proxy.get_current_active_user] = lambda: object()
    console = TestClient(app)

    before = httpx.get(f"{bifrost}/api/providers/openai", timeout=10).json()
    key = httpx.post(
        f"{bifrost}/api/providers/openai/keys",
        json={
            "name": f"chain-{uuid.uuid4().hex[:8]}",
            "value": "sk-chain-test",
            "weight": 1,
            "models": ["*"],
            "enabled": True,
        },
        timeout=10,
    ).json()
    stub = _Stub()
    host = os.getenv("BIFROST_STUB_HOST", "host.docker.internal")
    try:
        saved = console.put(
            "/api/bifrost/providers/openai",
            json={"network_config": {"base_url": f"http://{host}:{stub.server_port}/v1"}},
        )
        assert saved.status_code == 200, saved.text

        doc = httpx.get(f"{bifrost}/api/providers/openai", timeout=10).json()
        assert doc["network_config"]["base_url"] == f"http://{host}:{stub.server_port}"
        assert doc["network_config"]["allow_private_network"] is True

        reply = _chat(bifrost)
        assert reply.status_code == 200, reply.text
        assert reply.json()["choices"][0]["message"]["content"] == "from-the-stub"
        assert stub.paths == ["/v1/chat/completions"]
    finally:
        stub.shutdown()
        console.put(
            "/api/bifrost/providers/openai", json={"network_config": {"base_url": ""}}
        )
        httpx.delete(f"{bifrost}/api/providers/openai/keys/{key['id']}", timeout=10)
        after = httpx.get(f"{bifrost}/api/providers/openai", timeout=10).json()
        assert after["network_config"].get("base_url") == before["network_config"].get(
            "base_url"
        )
