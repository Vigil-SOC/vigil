"""Unit tests for core.llm.bifrost.admin sync helpers.

Covers the key upsert path against Bifrost's ``/api/providers/{name}/keys``
subresource and the OpenAI-compatible ``network_config.base_url`` push on
the provider document. The recording client must distinguish those two
GETs — a shared payload is what hid the keys-subresource contract change.
httpx is monkeypatched via a fake client so tests don't depend on a
running Bifrost.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.llm.bifrost import admin as bifrost_admin  # noqa: E402

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

    def __init__(
        self,
        get_payload=None,
        get_status=200,
        put_status=200,
        post_status=200,
        delete_status=200,
        write_payload=None,
        provider_payload=None,
        provider_status=200,
    ):
        self._get_payload = get_payload
        self._get_status = get_status
        self._put_status = put_status
        self._post_status = post_status
        self._delete_status = delete_status
        self._write_payload = write_payload if write_payload is not None else {}
        # Provider document is a different resource from /keys. Default to an
        # empty doc (no keys) so a shared payload cannot hide that split.
        self._provider_payload = (
            provider_payload if provider_payload is not None else {}
        )
        self._provider_status = provider_status
        self.calls: List[Dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _record(self, method, status, url, kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _FakeResp(status, self._write_payload, "")

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        if "/keys" in url:
            return _FakeResp(self._get_status, self._get_payload)
        return _FakeResp(self._provider_status, self._provider_payload)

    def put(self, url, **kwargs):
        return self._record("PUT", self._put_status, url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", self._post_status, url, kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", self._delete_status, url, kwargs)


_SECRET = "sk-ant-real-secret"


# Bifrost's read shape: one key, secret masked, allow-list alongside it.
def _key_doc(models=None, value="sk-ant-****-masked", key_id="key-1"):
    return {
        "keys": [
            {
                "id": key_id,
                "name": "default-anthropic-key",
                "value": {"value": value, "type": "plain_text"},
                "models": models if models is not None else ["old"],
                "weight": 1,
                "enabled": True,
                "config_hash": "abc123",
                "status": "unknown",
            }
        ],
        "total": 1,
    }


def _provider_doc(base_url=None, extra_network=None, **extra):
    network = {"default_request_timeout_in_seconds": 30, "max_retries": 3}
    if extra_network:
        network.update(extra_network)
    if base_url is not None:
        network["base_url"] = base_url
    doc = {
        "name": "openai",
        "network_config": network,
        "concurrency_and_buffer_size": {"concurrency": 1000, "buffer_size": 5000},
        "send_back_raw_request": False,
    }
    doc.update(extra)
    return doc


def test_sync_provider_models_skips_empty_list():
    # Must not even hit the admin API — refuses to wipe the allow-list.
    with patch.object(bifrost_admin.httpx, "Client", lambda: _RecordingClient()):
        assert (
            bifrost_admin.sync_provider_models("anthropic", [], key_value=_SECRET)
            is False
        )


def test_sync_provider_models_dedupes_and_preserves_order():
    rec = _RecordingClient(get_payload=_key_doc())

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
            key_value=_SECRET,
        )
    assert ok is True

    put = [c for c in rec.calls if c["method"] == "PUT"][0]
    body = put["kwargs"]["json"]
    assert body["models"] == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-3-5",
    ]


def test_keys_are_read_from_the_keys_subresource():
    """Regression: keys are not part of the provider document.

    Reading them from ``/api/providers/{name}`` yields no ``keys`` field at
    all, which silently disabled every sync path in this module.
    """
    rec = _RecordingClient(get_payload=_key_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=_SECRET
        )

    get = [c for c in rec.calls if c["method"] == "GET"][0]
    assert get["url"].endswith("/api/providers/anthropic/keys")


def test_write_never_echoes_the_masked_value_back():
    """Regression: Bifrost masks secrets on read and accepts the mask on write.

    A read-modify-write would store ``sk-ant-****-masked`` *as the credential*
    with a 200 and no error, silently breaking a working provider. Every write
    must carry the value from the secrets store instead.
    """
    masked = "sk-ant-****-masked"
    rec = _RecordingClient(get_payload=_key_doc(value=masked))

    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=_SECRET
        )

    body = [c for c in rec.calls if c["method"] == "PUT"][0]["kwargs"]["json"]
    assert body["value"] == _SECRET
    # And not the wrapper either: Bifrost stores that verbatim as the credential,
    # which reads back as its own masked JSON and 401s every call.
    assert not isinstance(body["value"], dict)
    assert masked not in str(body)


def test_write_strips_readback_only_fields():
    rec = _RecordingClient(get_payload=_key_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=_SECRET
        )

    body = [c for c in rec.calls if c["method"] == "PUT"][0]["kwargs"]["json"]
    for field in ("id", "config_hash", "status", "description"):
        assert field not in body


def test_sync_provider_models_returns_false_when_provider_missing():
    rec = _RecordingClient(get_status=404, get_payload=None)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=_SECRET
        )
    assert ok is False
    assert not any(c["method"] in ("PUT", "POST") for c in rec.calls)


def test_sync_provider_models_returns_false_on_put_error():
    rec = _RecordingClient(get_payload=_key_doc(), put_status=500)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models("openai", ["gpt-4o"], key_value=_SECRET)
    assert ok is False


def test_sync_provider_models_creates_key_when_none_exists():
    # Bifrost accepts key creation at runtime, so an unconfigured provider is
    # not a dead end — no placeholder slot has to be seeded up front.
    rec = _RecordingClient(get_payload={"keys": [], "total": 0})
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=_SECRET
        )
    assert ok is True

    post = [c for c in rec.calls if c["method"] == "POST"][0]
    assert post["url"].endswith("/api/providers/anthropic/keys")
    assert post["kwargs"]["json"]["models"] == ["claude-opus-4-7"]


def test_sync_provider_models_skips_when_no_secret_configured():
    # Every write must carry the credential, so there is nothing to sync.
    rec = _RecordingClient(get_payload=_key_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models(
            "anthropic", ["claude-opus-4-7"], key_value=None
        )
    assert ok is False
    assert not rec.calls


def test_sync_provider_models_leaves_ollama_alone():
    # Ollama's key holds a masked URL, not a secret we own, and its allow-list
    # is the static wildcard.
    rec = _RecordingClient(get_payload=_key_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_models(
            "ollama", ["llama3.2:1b"], key_value=None
        )
    assert ok is True
    assert not rec.calls


def test_push_provider_key_updates_existing_key_in_place():
    rec = _RecordingClient(get_payload=_key_doc(models=["claude-opus-4-7"]))
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.push_provider_key("anthropic", "sk-ant-new") is True

    put = [c for c in rec.calls if c["method"] == "PUT"][0]
    assert put["url"].endswith("/api/providers/anthropic/keys/key-1")
    body = put["kwargs"]["json"]
    # Bare, not wrapped: the wrapper is accepted with a 200 and stored as the
    # credential itself, which 401s every call afterwards.
    assert body["value"] == "sk-ant-new"
    # Carries the existing allow-list forward rather than wiping it.
    assert body["models"] == ["claude-opus-4-7"]


def test_push_provider_key_creates_key_when_absent():
    rec = _RecordingClient(get_payload={"keys": [], "total": 0})
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.push_provider_key("anthropic", "sk-ant-new") is True

    post = [c for c in rec.calls if c["method"] == "POST"][0]
    assert post["url"].endswith("/api/providers/anthropic/keys")
    assert post["kwargs"]["json"]["value"] == "sk-ant-new"


def test_push_provider_key_deletes_on_empty_value():
    # Bifrost rejects a key with an empty value, so "cleared" is a deletion.
    rec = _RecordingClient(get_payload=_key_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.push_provider_key("anthropic", "") is True

    delete = [c for c in rec.calls if c["method"] == "DELETE"][0]
    assert delete["url"].endswith("/api/providers/anthropic/keys/key-1")
    assert not any(c["method"] in ("PUT", "POST") for c in rec.calls)


def test_push_provider_key_reports_false_on_write_error():
    # llm_providers surfaces this to the user, so a failed push must not
    # report success.
    rec = _RecordingClient(get_payload=_key_doc(), put_status=500)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.push_provider_key("anthropic", "sk-ant-new") is False


# ---------------------------------------------------------------------------
# sync_all_provider_models — canonical single-writer path
# ---------------------------------------------------------------------------


class _FakeProviderRow:
    def __init__(self, provider_id, provider_type, base_url=None, is_default=False):
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.base_url = base_url
        self.api_key_ref = None
        self.config = {}
        self.is_active = True
        self.is_default = is_default


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
        "core.storage.connection.get_db_manager",
        lambda: fake_db,
        raising=False,
    )


def _reset_registry():
    from core.llm.providers import registry as model_registry

    model_registry._MODEL_LIST_CACHE.clear()
    model_registry._EXTRA_IDS.clear()
    model_registry.clear_live_meta()


def test_sync_all_populates_dropdown_cache_and_bifrost_allowlist(monkeypatch):
    """The whole point of the refactor: one call writes the dropdown cache
    AND Bifrost's allow-list — they can't drift because they come from
    the same iteration over the same upstream fetch."""
    from core.llm.bifrost import admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    # Stub the discovery fetch — returns two live models.
    async def fake_fetch_row(row_dict, discovery, key=None):
        return [_M("claude-opus-4-7"), _M("claude-haiku-4-5-20251001")]

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    # Capture Bifrost PUTs.
    pushed = {}

    def fake_push(provider_type, model_ids, *, key_value=None):
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
    from core.llm.providers.registry import _MODEL_LIST_CACHE

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
    from core.llm.bifrost import admin as ba

    _reset_registry()

    rows = [
        _FakeProviderRow("ant-dev", "anthropic"),
        _FakeProviderRow("ant-prod", "anthropic"),
    ]
    _patch_db(monkeypatch, rows)

    async def fake_fetch_row(row_dict, discovery, key=None):
        if row_dict["provider_id"] == "ant-dev":
            return [_M("claude-opus-4-7"), _M("claude-haiku-4-5-20251001")]
        return [_M("claude-opus-4-7"), _M("claude-sonnet-4-6")]

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    pushed = {}

    def fake_push(provider_type, model_ids, *, key_value=None):
        pushed[provider_type] = list(model_ids)
        return True

    monkeypatch.setattr(ba, "sync_provider_models", fake_push)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")  # no extras for this test

    import asyncio

    asyncio.run(ba.sync_all_provider_models())

    from core.llm.providers.registry import _MODEL_LIST_CACHE

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
    from core.llm.bifrost import admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    async def fake_fetch_row(row_dict, discovery, key=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)

    pushed = {}

    def fake_push(provider_type, model_ids, *, key_value=None):
        pushed[provider_type] = list(model_ids)
        return True

    monkeypatch.setattr(ba, "sync_provider_models", fake_push)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "legacy-1")

    import asyncio

    asyncio.run(ba.sync_all_provider_models())

    from core.llm.providers.registry import _MODEL_LIST_CACHE

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
    from core.llm.bifrost import admin as ba

    _reset_registry()

    rows = [_FakeProviderRow("ant-default", "anthropic")]
    _patch_db(monkeypatch, rows)

    import asyncio

    fetch_calls = {"n": 0}
    gate = asyncio.Event()

    async def slow_fake_fetch_row(row_dict, discovery, key=None):
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
    from core.llm.providers.registry import _MODEL_LIST_CACHE

    _MODEL_LIST_CACHE.clear()
    _MODEL_LIST_CACHE["p1"] = ["a", "b"]

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
    _MODEL_LIST_CACHE.clear()


def test_fetch_meta_for_row_ollama_bypasses_ssrf_ip_gate():
    """The ollama branch must pass ``allow_loopback=True``.

    The row's ``base_url`` was persisted by a ``settings.write`` admin
    (shape-validated at save time), and self-hosted Ollama on a
    loopback/private address is the expected deployment. Without the
    flag, the scheduled sync re-runs the SSRF IP gate and fails with
    "resolved address ... is disallowed: private address" for any
    RFC1918 host — even though the admin-gated discover-models and
    test endpoints reach the same URL fine.
    """
    import asyncio

    calls: Dict[str, Any] = {}

    class _FakeDiscovery:
        @staticmethod
        async def fetch_ollama_models(base_url=None, *, allow_loopback=False):
            calls["base_url"] = base_url
            calls["allow_loopback"] = allow_loopback
            return []

    row = {
        "provider_id": "ollama",
        "provider_type": "ollama",
        "base_url": "http://10.64.201.1:11434",
        "api_key_ref": None,
        "config": {},
    }

    out = asyncio.run(bifrost_admin._fetch_meta_for_row(row, _FakeDiscovery))

    assert out == []
    assert calls == {
        "base_url": "http://10.64.201.1:11434",
        "allow_loopback": True,
    }


# ---------------------------------------------------------------------------
# sync_provider_base_url — OpenAI-compatible custom host (#586)
# ---------------------------------------------------------------------------


def _provider_puts(rec):
    return [c for c in rec.calls if c["method"] == "PUT" and "/keys" not in c["url"]]


def _key_writes(rec):
    return [
        c for c in rec.calls if c["method"] in ("PUT", "POST") and "/keys" in c["url"]
    ]


def test_recording_client_distinguishes_provider_document_from_keys():
    """The test double must not share a payload across the two GETs.

    A single payload made provider-document reads look like they had keys,
    which is the contract the gateway dropped and the suite kept green.
    """
    rec = _RecordingClient(
        get_payload=_key_doc(models=["anthropic/claude-sonnet-5"]),
        provider_payload=_provider_doc(),
    )
    with rec as client:
        prov = client.get("http://localhost:8080/api/providers/openai")
        keys = client.get("http://localhost:8080/api/providers/openai/keys")
    assert "keys" not in prov.json()
    assert "network_config" in prov.json()
    assert keys.json()["keys"][0]["models"] == ["anthropic/claude-sonnet-5"]


def test_sync_provider_base_url_trims_trailing_v1():
    rec = _RecordingClient(provider_payload=_provider_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_base_url(
            "openai", "https://openrouter.ai/api/v1"
        )
    assert ok is True
    put = _provider_puts(rec)[0]
    assert put["url"].endswith("/api/providers/openai")
    assert "/keys" not in put["url"]
    body = put["kwargs"]["json"]
    assert body["network_config"]["base_url"] == "https://openrouter.ai/api"
    assert "keys" not in body


def test_sync_provider_base_url_keeps_path_prefix_without_v1_suffix():
    rec = _RecordingClient(provider_payload=_provider_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_base_url(
            "openai", "https://litellm.example/openai"
        )
    assert ok is True
    body = _provider_puts(rec)[0]["kwargs"]["json"]
    assert body["network_config"]["base_url"] == "https://litellm.example/openai"


def test_sync_provider_base_url_preserves_unrelated_network_config():
    rec = _RecordingClient(
        provider_payload=_provider_doc(
            extra_network={"retry_backoff_initial": 500, "insecure_skip_verify": False}
        )
    )
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_base_url("openai", "https://together.xyz/v1")
    network = _provider_puts(rec)[0]["kwargs"]["json"]["network_config"]
    assert network["base_url"] == "https://together.xyz"
    assert network["default_request_timeout_in_seconds"] == 30
    assert network["max_retries"] == 3
    assert network["retry_backoff_initial"] == 500
    assert network["insecure_skip_verify"] is False


def test_sync_provider_base_url_is_idempotent_for_v1_and_bare_forms():
    for stored, existing in (
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api"),
    ):
        rec = _RecordingClient(provider_payload=_provider_doc(base_url=existing))
        with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
            ok = bifrost_admin.sync_provider_base_url("openai", stored)
        assert ok is True
        assert _provider_puts(rec) == []
        assert any(c["method"] == "GET" and "/keys" not in c["url"] for c in rec.calls)


def test_sync_provider_base_url_skips_anthropic_and_ollama():
    rec = _RecordingClient(provider_payload=_provider_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert (
            bifrost_admin.sync_provider_base_url(
                "anthropic", "https://proxy.example/anthropic/v1"
            )
            is True
        )
        assert (
            bifrost_admin.sync_provider_base_url("ollama", "http://localhost:11434")
            is True
        )
    assert rec.calls == []


def test_sync_provider_base_url_skips_empty_and_stock_openai():
    rec = _RecordingClient(provider_payload=_provider_doc())
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.sync_provider_base_url("openai", None) is True
        assert bifrost_admin.sync_provider_base_url("openai", "") is True
        assert (
            bifrost_admin.sync_provider_base_url("openai", "https://api.openai.com/v1")
            is True
        )
    assert _provider_puts(rec) == []
    assert all(c["method"] == "GET" and "/keys" not in c["url"] for c in rec.calls)


def test_sync_provider_base_url_returns_false_when_provider_missing():
    rec = _RecordingClient(provider_status=404, provider_payload=None)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_base_url(
            "openai", "https://openrouter.ai/api/v1"
        )
    assert ok is False
    assert _provider_puts(rec) == []


def test_sync_provider_base_url_returns_false_on_put_error():
    rec = _RecordingClient(provider_payload=_provider_doc(), put_status=500)
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_base_url(
            "openai", "https://openrouter.ai/api/v1"
        )
    assert ok is False


def test_sync_provider_base_url_clears_stale_custom_host_when_reverting_to_stock():
    rec = _RecordingClient(
        provider_payload=_provider_doc(base_url="https://openrouter.ai/api")
    )
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        ok = bifrost_admin.sync_provider_base_url("openai", "https://api.openai.com/v1")
    assert ok is True
    network = _provider_puts(rec)[0]["kwargs"]["json"]["network_config"]
    assert "base_url" not in network
    assert network["default_request_timeout_in_seconds"] == 30


def test_base_url_put_omits_readback_only_fields():
    rec = _RecordingClient(
        provider_payload=_provider_doc(
            status="active",
            config_hash="abc",
            keys=[{"id": "should-not-be-echoed", "models": ["gpt-4o"]}],
        )
    )
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_base_url("openai", "https://openrouter.ai/api/v1")
    body = _provider_puts(rec)[0]["kwargs"]["json"]
    assert "keys" not in body
    assert "status" not in body
    assert "config_hash" not in body
    assert "name" not in body
    assert "network_config" in body
    assert "concurrency_and_buffer_size" in body


def test_base_url_refresh_keeps_namespaced_model_on_key_allow_list():
    rec = _RecordingClient(
        get_payload=_key_doc(models=["anthropic/claude-sonnet-5", "gpt-4o"]),
        provider_payload=_provider_doc(),
    )
    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        assert bifrost_admin.sync_provider_base_url(
            "openai", "https://openrouter.ai/api/v1"
        )
        assert bifrost_admin.sync_provider_models(
            "openai",
            ["anthropic/claude-sonnet-5", "gpt-4o"],
            key_value=_SECRET,
        )
    assert "keys" not in _provider_puts(rec)[0]["kwargs"]["json"]
    key_put = _key_writes(rec)[0]
    assert key_put["kwargs"]["json"]["models"] == [
        "anthropic/claude-sonnet-5",
        "gpt-4o",
    ]
    assert key_put["kwargs"]["json"]["value"] == _SECRET
    assert not isinstance(key_put["kwargs"]["json"]["value"], dict)


def test_connection_test_success_leaves_bifrost_on_stock_host():
    """Regression: discovery (and the connection probe) dial the custom host
    directly. A green test is therefore not proof the gateway will route
    there — that was the #586 failure mode.
    """
    rec = _RecordingClient(provider_payload=_provider_doc())

    class _FakeDiscovery:
        @staticmethod
        async def fetch_openai_models(*_a, **_kw):
            return []

    row = {
        "provider_id": "openrouter",
        "provider_type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_ref": None,
        "config": {},
    }

    import asyncio

    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        asyncio.run(
            bifrost_admin._fetch_meta_for_row(row, _FakeDiscovery, key="sk-or-test")
        )

    # Direct discovery never writes the gateway document.
    assert rec.calls == []
    assert "base_url" not in rec._provider_payload["network_config"]

    with patch.object(bifrost_admin.httpx, "Client", lambda: rec):
        bifrost_admin.sync_provider_base_url("openai", row["base_url"])

    assert (
        _provider_puts(rec)[0]["kwargs"]["json"]["network_config"]["base_url"]
        == "https://openrouter.ai/api"
    )


def test_sync_all_pushes_default_openai_row_base_url(monkeypatch):
    from core.llm.bifrost import admin as ba

    _reset_registry()

    rows = [
        _FakeProviderRow(
            "stock", "openai", base_url="https://api.openai.com/v1", is_default=False
        ),
        _FakeProviderRow(
            "openrouter",
            "openai",
            base_url="https://openrouter.ai/api/v1",
            is_default=True,
        ),
    ]
    _patch_db(monkeypatch, rows)

    async def fake_fetch_row(row_dict, discovery, key=None):
        return [_M("gpt-4o"), _M("anthropic/claude-sonnet-5")]

    rec = _RecordingClient(
        get_payload=_key_doc(models=["gpt-4o"]),
        provider_payload=_provider_doc(),
    )
    monkeypatch.setattr(ba, "_fetch_meta_for_row", fake_fetch_row)
    monkeypatch.setattr(ba, "_resolve_row_key", lambda row: _SECRET)
    monkeypatch.setattr(ba.httpx, "Client", lambda: rec)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")

    import asyncio

    result = asyncio.run(ba.sync_all_provider_models())

    assert result["bifrost_base_url"]["openai"] is True
    assert (
        _provider_puts(rec)[0]["kwargs"]["json"]["network_config"]["base_url"]
        == "https://openrouter.ai/api"
    )
    key_put = _key_writes(rec)[0]
    assert "anthropic/claude-sonnet-5" in key_put["kwargs"]["json"]["models"]
    _reset_registry()


# ---------------------------------------------------------------------------
# default_model_for_provider_type — ollama floors to a chat model (#1003)
# ---------------------------------------------------------------------------


def _ollama_meta(*specs):
    """``ModelMeta`` stand-ins from ``(id, is_embedding)`` pairs.

    ``is_embedding=None`` leaves the key absent, as ``fetch_ollama_models``
    does for a server that reports no capabilities.
    """
    from core.llm.providers.discovery import ModelMeta

    return [
        ModelMeta(
            id=mid,
            display_name=mid,
            capabilities={} if emb is None else {"is_embedding": emb},
        )
        for mid, emb in specs
    ]


def _ollama_floor(monkeypatch, models):
    import asyncio

    async def _fake_list(base_url, discovery=None):
        return models

    monkeypatch.setattr(bifrost_admin, "_list_ollama_models", _fake_list)
    return asyncio.run(bifrost_admin.default_model_for_provider_type("ollama"))


def test_ollama_floor_skips_embedding_model_listed_first(monkeypatch):
    models = _ollama_meta(
        ("nomic-embed-text:latest", True),
        ("qwen2.5:14b", False),
        ("llama3.1:8b", False),
    )
    assert _ollama_floor(monkeypatch, models) == "qwen2.5:14b"


def test_ollama_floor_skips_embedding_model_listed_last(monkeypatch):
    models = _ollama_meta(
        ("qwen2.5:14b", False),
        ("llama3.1:8b", False),
        ("nomic-embed-text:latest", True),
    )
    assert _ollama_floor(monkeypatch, models) == "qwen2.5:14b"


def test_ollama_floor_is_none_when_only_embedding_models_pulled(monkeypatch, caplog):
    models = _ollama_meta(
        ("nomic-embed-text:latest", True),
        ("mxbai-embed-large:latest", True),
    )
    with caplog.at_level("WARNING"):
        assert _ollama_floor(monkeypatch, models) is None
    assert "No pulled model to floor a mirrored ollama row to" in caplog.text


def test_ollama_floor_falls_back_to_name_when_capability_flag_absent(monkeypatch):
    models = _ollama_meta(
        ("nomic-embed-text:latest", None),
        ("llama3.1:8b", None),
    )
    assert _ollama_floor(monkeypatch, models) == "llama3.1:8b"


def test_ollama_floor_prefers_mid_tier_chat_model(monkeypatch):
    """The surviving ids go through ``_preferred_floor``, not index 0."""
    models = _ollama_meta(
        ("nomic-embed-text:latest", True),
        ("qwen2.5:72b", False),
        ("mistral-small:latest", False),
    )
    assert _ollama_floor(monkeypatch, models) == "mistral-small:latest"


def test_gemini_floor_prefers_latest_alias_over_retired_pin(monkeypatch):
    """Retired flash pins listed ahead of the alias must not win the floor (#1122)."""
    import asyncio

    from core.llm.providers.discovery import ModelMeta

    async def _fake_catalogue(provider_type):
        return [
            ModelMeta(id=mid, display_name=mid)
            for mid in ("gemini-2.0-flash", "gemini-2.5-pro", "gemini-flash-latest")
        ]

    monkeypatch.setattr(bifrost_admin, "fetch_catalogue_models", _fake_catalogue)
    assert (
        asyncio.run(bifrost_admin.default_model_for_provider_type("gemini"))
        == "gemini-flash-latest"
    )


def test_servable_models_excludes_embedding_ids(monkeypatch):
    """So ``_upsert_row`` corrects a row already floored to an embedding model."""
    import asyncio

    from core.llm.bifrost import mirror

    models = _ollama_meta(
        ("nomic-embed-text:latest", True),
        ("llama3.1:8b", False),
    )

    async def _fake_list(base_url, discovery=None):
        return models

    monkeypatch.setattr(bifrost_admin, "_list_ollama_models", _fake_list)
    assert asyncio.run(mirror._servable_models("ollama")) == {"llama3.1:8b"}
