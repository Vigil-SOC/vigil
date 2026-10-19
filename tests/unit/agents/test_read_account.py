# read_account forwards serve's account of a run, held to the contract on the way.

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from core.agents import projections
from core.storage.schemas.case_account import dump_account

pytestmark = pytest.mark.unit

RUN = "5a2c2d3e-0000-4000-8000-000000000891"
FIXTURE = (
    Path(__file__).parents[3]
    / "services/agent/tests/fixtures/account/investigate-01c1d889.json"
)


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
async def test_the_account_comes_back_as_serve_wrote_it(monkeypatch):
    written = json.loads(FIXTURE.read_text())
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=written))

    account = await projections.read_account(RUN)

    assert account is not None
    assert dump_account(account) == written
    assert seen[0].url.path == f"/runs/{RUN}/account"


@pytest.mark.asyncio
async def test_serve_404_is_none(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(404, json={"detail": "missing"}))

    assert await projections.read_account(RUN) is None


@pytest.mark.asyncio
async def test_an_account_that_breaks_the_contract_raises(monkeypatch):
    broken = {"schema_version": 1, "runs": [], "verdict": None}
    _serve(monkeypatch, lambda _r: httpx.Response(200, json=broken))

    with pytest.raises(ValidationError):
        await projections.read_account(RUN)


@pytest.mark.asyncio
async def test_any_other_status_raises_with_the_body(monkeypatch):
    _serve(monkeypatch, lambda _r: httpx.Response(502, text="ledger would not open"))

    with pytest.raises(RuntimeError, match="answered 502: ledger would not open"):
        await projections.read_account(RUN)
