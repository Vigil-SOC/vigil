# read_replay against serve's three answers: a report, "nothing to replay", and a
# failure the operator is owed. Unlike the polled reads, only the middle one is None.

from __future__ import annotations

import httpx
import pytest

from core.agents import projections

pytestmark = pytest.mark.unit

RUN = "5a2c2d3e-0000-4000-8000-000000000891"
REPORT = {"hunt_id": "hunt-1", "decisions": [], "recalled": []}


def _serve(monkeypatch, handler):
    seen = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(projections, "_headers", lambda: {"Authorization": "Bearer t"})
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_handle), **kw),
    )
    return seen


@pytest.mark.asyncio
async def test_a_report_comes_back_and_decision_id_rides_the_query(monkeypatch):
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=REPORT))

    assert await projections.read_replay(RUN, "dec-1") == REPORT
    assert await projections.read_replay(RUN) == REPORT

    assert seen[0].url.path == f"/runs/{RUN}/replay"
    assert seen[0].url.params["decision_id"] == "dec-1"
    assert "decision_id" not in seen[1].url.params
    assert seen[0].headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_serve_404_is_none(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(404, json={"detail": "no hunt"}))

    assert await projections.read_replay(RUN) is None


@pytest.mark.asyncio
async def test_any_other_status_raises_with_the_body(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(502, text="ledger would not fold"))

    with pytest.raises(RuntimeError, match="answered 502: ledger would not fold"):
        await projections.read_replay(RUN)


@pytest.mark.asyncio
async def test_an_unreachable_agent_layer_raises(monkeypatch):
    def _down(_request):
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, _down)

    with pytest.raises(RuntimeError, match="could not reach the agent layer"):
        await projections.read_replay(RUN)
