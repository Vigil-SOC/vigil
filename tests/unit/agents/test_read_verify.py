# read_verify forwards serve's chain walk. Python does not hash it.

from __future__ import annotations

import httpx
import pytest

from core.agents import projections

pytestmark = pytest.mark.unit

RUN = "5a2c2d3e-0000-4000-8000-000000000891"
RESULT = {"ok": True, "events": 2, "runs": 1}


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
async def test_the_chain_walk_comes_back(monkeypatch):
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=RESULT))

    assert await projections.read_verify(RUN) == RESULT
    assert seen[0].url.path == f"/runs/{RUN}/verify"
    assert seen[0].headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_serve_404_is_none(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(404, json={"detail": "missing"}))

    assert await projections.read_verify(RUN) is None


@pytest.mark.asyncio
async def test_any_other_status_raises_with_the_body(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(502, text="ledger would not open"))

    with pytest.raises(RuntimeError, match="answered 502: ledger would not open"):
        await projections.read_verify(RUN)
