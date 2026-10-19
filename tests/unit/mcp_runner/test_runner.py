"""MCP runner: control API and streamable-HTTP bridge against a stdio fixture.

In process, no sockets: a streaming ASGI transport carries the bridge's SSE
responses, which httpx's own ASGITransport would buffer until the stream ends.
Runs the not-root path (no setpriv); the uid switch is checked live.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from services.mcp_runner.catalog import Catalog
from services.mcp_runner.main import create_app

pytestmark = pytest.mark.unit

TOKEN = "t0ken"
FIXTURE = Path(__file__).with_name("fixture_server.py")


class _Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks, disconnect):
        self._chunks, self._disconnect = chunks, disconnect

    async def __aiter__(self):
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self._disconnect.set()
        self._chunks.close()


class StreamingASGITransport(httpx2.AsyncBaseTransport):
    def __init__(self, app, tg):
        self.app, self.tg = app, tg

    async def handle_async_request(self, request):
        body = await request.aread()
        tx, rx = anyio.create_memory_object_stream(100)
        start: dict = {}
        started, disconnect = anyio.Event(), anyio.Event()
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                start.update(message)
                started.set()
            elif message["type"] == "http.response.body":
                try:
                    await tx.send(message.get("body", b""))
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    pass
                if not message.get("more_body"):
                    tx.close()

        async def run():
            try:
                await self.app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "http_version": "1.1",
                        "method": request.method,
                        "scheme": "http",
                        "path": request.url.path,
                        "raw_path": request.url.raw_path.split(b"?")[0],
                        "query_string": request.url.query,
                        "root_path": "",
                        "headers": [(k.lower(), v) for k, v in request.headers.raw],
                        "client": ("127.0.0.1", 1),
                        "server": ("mcp-runner", 6991),
                    },
                    receive,
                    send,
                )
            finally:
                started.set()
                tx.close()

        self.tg.start_soon(run)
        await started.wait()
        return httpx2.Response(
            start.get("status", 500),
            headers=start.get("headers", []),
            stream=_Stream(rx, disconnect),
        )


@pytest.fixture
def catalog(tmp_path):
    (tmp_path / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "_comment": "section header",
                    "fixture": {
                        "command": "python3",
                        "args": [str(FIXTURE)],
                        "cwd": "${workspaceFolder}",
                        "env": {"GREETING": "${GREETING:-hi}", "FROM_ARGS": "x"},
                    },
                    "broken": {
                        "command": "python3",
                        "args": [
                            "-c",
                            "import sys; sys.stderr.write('boom\\n'); sys.exit(3)",
                        ],
                    },
                }
            }
        )
    )
    return Catalog(tmp_path)


@asynccontextmanager
async def runner(catalog, tmp_path, token=TOKEN):
    app = create_app(token, catalog, state_dir=tmp_path / "state", switch_uid=False)
    supervisor = app.app.state.supervisor
    async with anyio.create_task_group() as tg:
        async with supervisor:
            transport = StreamingASGITransport(app, tg)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://mcp-runner", headers=headers
            ) as http:
                yield http, supervisor
        tg.cancel_scope.cancel()


@asynccontextmanager
async def mcp_session(http, name="fixture"):
    async with streamable_http_client(
        f"http://mcp-runner/servers/{name}/mcp", http_client=http
    ) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def _text(session, tool, **args):
    result = await session.call_tool(tool, args)
    return result.content[0].text


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_empty_token_answers_503_everywhere(catalog, tmp_path):
    async with runner(catalog, tmp_path, token="") as (http, _):
        for method, path in [
            ("GET", "/servers"),
            ("PUT", "/servers/fixture"),
            ("DELETE", "/servers/fixture"),
            ("POST", "/servers/fixture/mcp"),
        ]:
            assert (await http.request(method, path)).status_code == 503


async def test_wrong_token_is_401(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, _):
        r = await http.get("/servers", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


async def test_unknown_name_is_404(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, _):
        assert (await http.put("/servers/nope", json={})).status_code == 404
        assert (await http.delete("/servers/nope")).status_code == 404
        assert (await http.post("/servers/nope/mcp", json={})).status_code == 404
        # In the catalog but not running.
        assert (await http.post("/servers/fixture/mcp", json={})).status_code == 503


async def test_put_bridges_and_ignores_command(catalog, tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUNNER_TOKEN", TOKEN)
    async with runner(catalog, tmp_path) as (http, _):
        r = await http.put(
            "/servers/fixture",
            json={"env": {"GREETING": "hello"}, "command": "/bin/false"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["action"] == "started" and r.json()["running"]
        assert "GREETING" not in r.text  # no env in the response

        async with mcp_session(http) as session:
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"env", "sleep"}
            assert await _text(session, "env", name="GREETING") == "hello"
            assert await _text(session, "env", name="FROM_ARGS") == "x"
            assert await _text(session, "env", name="HOME") == str(
                tmp_path / "state" / "fixture"
            )
            # The runner's own environment never reaches the child.
            assert await _text(session, "env", name="MCP_RUNNER_TOKEN") == "<unset>"

        status = (await http.get("/servers")).json()
        assert status["fixture"]["state"] == "running"
        assert status["broken"] == {"state": "stopped"}


async def test_put_noop_restart_and_delete(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, _):
        first = (await http.put("/servers/fixture", json={"env": {}})).json()
        again = (await http.put("/servers/fixture", json={"env": {}})).json()
        assert again["action"] == "unchanged" and again["pid"] == first["pid"]

        changed = (await http.put("/servers/fixture", json={"env": {"A": "1"}})).json()
        assert changed["action"] == "restarted" and changed["pid"] != first["pid"]
        assert _gone(first["pid"])

        async with mcp_session(http) as session:
            assert await _text(session, "env", name="A") == "1"

        assert (await http.delete("/servers/fixture")).status_code == 200
        assert _gone(changed["pid"])
        assert (await http.post("/servers/fixture/mcp", json={})).status_code == 503
        assert (await http.get("/servers")).json()["fixture"] == {"state": "stopped"}


async def test_calls_multiplex(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, _):
        await http.put("/servers/fixture", json={})
        async with mcp_session(http) as session:
            began = time.monotonic()
            async with anyio.create_task_group() as tg:
                for _ in range(2):
                    tg.start_soon(lambda: _text(session, "sleep", seconds=1))
            # Serialised behind one lock this would take two seconds.
            assert time.monotonic() - began < 1.8


@pytest.mark.parametrize("change", ["restart", "delete"])
async def test_stop_under_concurrent_calls(catalog, tmp_path, change):
    async with runner(catalog, tmp_path) as (http, _):
        old = (await http.put("/servers/fixture", json={})).json()
        outcomes = []

        async def slow_call():
            try:
                async with mcp_session(http) as session:
                    result = await session.call_tool("sleep", {"seconds": 30})
                    outcomes.append("error" if result.is_error else "ok")
            except Exception:
                outcomes.append("raised")

        with anyio.fail_after(15):
            async with anyio.create_task_group() as tg:
                for _ in range(3):
                    tg.start_soon(slow_call)
                await anyio.sleep(0.5)
                if change == "restart":
                    r = await http.put("/servers/fixture", json={"env": {"B": "2"}})
                    assert r.json()["action"] == "restarted"
                else:
                    await http.delete("/servers/fixture")
        assert outcomes and "ok" not in outcomes
        assert _gone(old["pid"])
        if change == "restart":
            async with mcp_session(http) as session:
                assert await _text(session, "env", name="B") == "2"


async def test_spawn_failure_reports_exit_code_and_stderr(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, _):
        r = await http.put("/servers/broken", json={})
        assert r.status_code == 502
        body = r.json()
        assert body["running"] is False and body["state"] == "failed"
        assert body["exit_code"] == 3
        assert "boom" in body["stderr"]


def test_launch_resolution(catalog, tmp_path):
    launch = catalog.launch("fixture", {"GREETING": "yo"}, "/home/x")
    assert launch.command == sys.executable
    assert launch.cwd == str(tmp_path)
    assert launch.env["GREETING"] == "yo"
    assert launch.env["HOME"] == "/home/x"
    assert launch.env["PYTHONPATH"] == str(tmp_path)
    # Catalog default when the request does not send it.
    assert catalog.launch("fixture", {}, "/h").env["GREETING"] == "hi"


async def test_loader_env_is_refused(catalog, tmp_path):
    async with runner(catalog, tmp_path) as (http, supervisor):
        r = await http.put("/servers/fixture", json={"env": {"LD_PRELOAD": "/x.so"}})
        assert r.status_code == 422
        assert "fixture" not in supervisor.children
