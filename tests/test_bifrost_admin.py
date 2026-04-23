"""Unit tests for services.bifrost_admin sync helpers (GH #139).

Verifies the new ``sync_provider_models`` path: empty-list guard, dedup,
GET-modify-PUT flow, and error handling. httpx is monkeypatched via a
fake client so tests don't depend on a running Bifrost.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services import bifrost_admin  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, status: int, payload: Any = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _RecordingClient:
    """Mimic ``httpx.Client`` as a context manager with recorded calls."""

    def __init__(self, get_payload=None, get_status=200, put_status=200):
        self._get_payload = get_payload
        self._get_status = get_status
        self._put_status = put_status
        self.calls: List[Dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        return _FakeResp(self._get_status, self._get_payload)

    def put(self, url, **kwargs):
        self.calls.append({"method": "PUT", "url": url, "kwargs": kwargs})
        return _FakeResp(self._put_status, None, "")


def test_sync_provider_models_skips_empty_list():
    # Must not even hit the admin API — refuses to wipe the allow-list.
    with patch.object(bifrost_admin.httpx, "Client", lambda: _RecordingClient()):
        assert bifrost_admin.sync_provider_models("anthropic", []) is False


def test_sync_provider_models_dedupes_and_preserves_order():
    provider_doc = {
        "keys": [
            {
                "value": {"value": "sk-...", "env_var": "", "from_env": False},
                "models": ["old"],
            }
        ]
    }
    rec = _RecordingClient(get_payload=provider_doc)

    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models(
            "anthropic",
            [
                "claude-opus-4-7",
                "claude-sonnet-4-6",
                "claude-opus-4-7",
                "",
                "claude-haiku-3-5",
            ],
        )
    assert ok is True

    put = [c for c in rec.calls if c["method"] == "PUT"][0]
    body = put["kwargs"]["json"]
    assert body["keys"][0]["models"] == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-3-5",
    ]


def test_sync_provider_models_returns_false_when_provider_missing():
    rec = _RecordingClient(get_status=404, get_payload=None)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models("anthropic", ["claude-opus-4-7"])
    assert ok is False
    # No PUT was attempted.
    assert not any(c["method"] == "PUT" for c in rec.calls)


def test_sync_provider_models_returns_false_on_put_error():
    provider_doc = {"keys": [{"models": []}]}
    rec = _RecordingClient(get_payload=provider_doc, put_status=500)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models("openai", ["gpt-4o"])
    assert ok is False


def test_sync_provider_models_returns_false_when_no_keys_slot():
    # A provider without any keys slot can't be updated.
    rec = _RecordingClient(get_payload={"keys": []})
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models("anthropic", ["claude-opus-4-7"])
    assert ok is False


# ---------------------------------------------------------------------------
# sync_all_provider_models — canonical single-writer path
# ---------------------------------------------------------------------------


class _FakeProviderRow:
    def __init__(self, provider_id, provider_type):
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.base_url = None
        self.api_key_ref = None
        self.config = {}
        self.is_active = True


class _FakeSessionScope:
    """Stand-in for db_manager.session_scope() context manager."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        class _S:
            def __init__(self, rows):
                self._rows = rows

            def query(self, model):
                class _Q:
                    def __init__(self, rows):
                        self._rows = rows

                    def filter(self, *_):
                        return self

                    def all(self):
                        return self._rows

                return _Q(self._rows)

        return _S(self._rows)

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeDBManager:
    def __init__(self, rows):
        self._rows = rows
        self._engine = object()  # truthy so initialize() isn't called

    def initialize(self):
        self._engine = object()

    def session_scope(self):
        return _FakeSessionScope(self._rows)


class _M:
    def __init__(self, mid):
        self.id = mid
        self.display_name = mid
        self.context_window = 0
        self.capabilities = {}


def _patch_db(monkeypatch, rows):
    fake_db = _FakeDBManager(rows)
    monkeypatch.setattr(
        "database.connection.get_db_manager",
        lambda: fake_db,
        raising=False,
    )


def _reset_registry():
    from services import model_registry

    model_registry._MODEL_LIST_CACHE.invalidate()
    model_registry._EXTRA_IDS.clear()
    model_registry.clear_live_meta()


def test_sync_all_populates_dropdown_cache_and_bifrost_allowlist(monkeypatch):
    """The whole point of the refactor: one call writes the dropdown cache
    AND Bifrost's allow-list — they can't drift because they come from
    the same iteration over the same upstream fetch."""
    from services import bifrost_admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    # Stub the discovery fetch — returns two live models.
    async def fake_fetch_row(row_dict, discovery):
        return [_M("claude-opus-4-7"), _M("claude-haiku-4-5-20251001")]

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    # Capture Bifrost PUTs.
    pushed = {}

    def fake_push(provider_type, model_ids):
        pushed[provider_type] = list(model_ids)
        return True

    monkeypatch.setattr(ba, "sync_provider_models", fake_push)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "claude-3-5-haiku-20241022")

    import asyncio

    result = asyncio.run(ba.sync_all_provider_models())

    # Bifrost allow-list — live list + extras, unioned.
    assert pushed["anthropic"] == [
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
    ]
    # Dropdown cache — same list, same row, same call.
    from services.model_registry import _MODEL_LIST_CACHE

    assert _MODEL_LIST_CACHE.get("ant-default") == [
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
    ]
    # Return shape includes both views.
    assert result["bifrost"]["anthropic"] is True
    assert result["models_by_provider"]["ant-default"] == [
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
    ]
    _reset_registry()


def test_sync_all_unions_across_same_type_providers(monkeypatch):
    """Two anthropic providers with different keys — per-row caches hold
    each row's own list; Bifrost allow-list is the union."""
    from services import bifrost_admin as ba

    _reset_registry()

    rows = [
        _FakeProviderRow("ant-dev", "anthropic"),
        _FakeProviderRow("ant-prod", "anthropic"),
    ]
    _patch_db(monkeypatch, rows)

    async def fake_fetch_row(row_dict, discovery):
        if row_dict["provider_id"] == "ant-dev":
            return [_M("claude-opus-4-7"), _M("claude-haiku-4-5-20251001")]
        return [_M("claude-opus-4-7"), _M("claude-sonnet-4-6")]

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    pushed = {}

    def fake_push(provider_type, model_ids):
        pushed[provider_type] = list(model_ids)
        return True

    monkeypatch.setattr(ba, "sync_provider_models", fake_push)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")  # no extras for this test

    import asyncio

    asyncio.run(ba.sync_all_provider_models())

    from services.model_registry import _MODEL_LIST_CACHE

    assert _MODEL_LIST_CACHE.get("ant-dev") == [
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
    ]
    assert _MODEL_LIST_CACHE.get("ant-prod") == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]
    # Union for Bifrost (order preserved, deduped).
    assert pushed["anthropic"] == [
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
    ]
    _reset_registry()


def test_sync_all_falls_back_when_all_fetches_fail(monkeypatch):
    """Every row's fetch failing → per-row cache gets bootstrap + extras,
    Bifrost allow-list gets the union."""
    from services import bifrost_admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    async def fake_fetch_row(row_dict, discovery):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    pushed = {}

    def fake_push(provider_type, model_ids):
        pushed[provider_type] = list(model_ids)
        return True

    monkeypatch.setattr(ba, "sync_provider_models", fake_push)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "legacy-1")

    import asyncio

    asyncio.run(ba.sync_all_provider_models())

    from services.model_registry import _MODEL_LIST_CACHE

    row_list = _MODEL_LIST_CACHE.get("ant-default")
    assert "claude-opus-4-7" in row_list  # from bootstrap
    assert "legacy-1" in row_list  # extras applied even on failure
    assert "legacy-1" in pushed["anthropic"]
    _reset_registry()


def test_sync_all_coalesces_concurrent_callers(monkeypatch):
    """Two simultaneous callers should share a single upstream fetch pass.

    Prevents a dropdown cold-load from doubling upstream load when it
    races the scheduled refresher's first tick.
    """
    from services import bifrost_admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    import asyncio

    fetch_calls = {"n": 0}
    gate = asyncio.Event()

    async def slow_fake_fetch_row(row_dict, discovery):
        fetch_calls["n"] += 1
        # Hold the sync open long enough for the second caller to join.
        await gate.wait()
        return [_M("claude-opus-4-7")]

    monkeypatch.setattr(ba, "_fetch_meta_for_row", slow_fake_fetch_row)
    monkeypatch.setattr(ba, "sync_provider_models", lambda *a, **kw: True)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")

    async def _race():
        task_a = asyncio.create_task(ba.sync_all_provider_models())
        # Yield so task_a enters the critical section and claims the slot.
        await asyncio.sleep(0)
        task_b = asyncio.create_task(ba.sync_all_provider_models())
        # Let task_b also start and try to join the in-flight future.
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(task_a, task_b)

    results = asyncio.run(_race())

    # Exactly one upstream fetch despite two callers.
    assert fetch_calls["n"] == 1
    # Both callers see the same result shape.
    assert results[0]["models_by_provider"] == results[1]["models_by_provider"]
    _reset_registry()


def test_cache_has_no_ttl(monkeypatch):
    """Cache entries are valid indefinitely until overwritten/invalidated.

    Before the drift-prevention refactor this had a 60s TTL that caused
    periodic latency spikes when the UI hit an expired entry.
    """
    from services.model_registry import _MODEL_LIST_CACHE

    _MODEL_LIST_CACHE.invalidate()
    _MODEL_LIST_CACHE.set("p1", ["a", "b"])

    # Pretend a long time has passed. Cache should still return the entry.
    import time as _time

    original = _time.time

    try:
        # Shift time far into the future. If a TTL lingered, .get() would
        # drop the entry.
        _time.time = lambda: original() + 10_000_000  # type: ignore[assignment]
        assert _MODEL_LIST_CACHE.get("p1") == ["a", "b"]
    finally:
        _time.time = original  # type: ignore[assignment]
    _MODEL_LIST_CACHE.invalidate()
