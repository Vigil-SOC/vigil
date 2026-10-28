"""/api/federation/sources and /health answer 503 when a store read fails."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.api.routers import federation as fed_router


class _Adapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_configured(self) -> bool:
        return True

    def default_interval(self) -> int:
        return 300


def _boom(*a, **k):
    raise RuntimeError("password=secret db down")


@pytest.fixture
def store_ok(monkeypatch):
    monkeypatch.setattr(
        fed_router.fed_registry, "list_adapters", lambda: [_Adapter("splunk")]
    )
    monkeypatch.setattr(fed_router.fed_store, "read_sources", lambda: [])
    monkeypatch.setattr(fed_router, "source_collection_lag", lambda: [])
    monkeypatch.setattr(
        fed_router.fed_store, "read_global_settings", lambda: {"enabled": True}
    )
    return monkeypatch


async def _assert_503(route):
    with pytest.raises(HTTPException) as exc:
        await route()
    assert exc.value.status_code == 503
    assert exc.value.detail == "Could not read federation sources"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        (fed_router.fed_store, "read_sources"),
        (fed_router, "source_collection_lag"),
        (fed_router.fed_store, "read_global_settings"),
    ],
    ids=["rows", "lag", "global"],
)
async def test_sources_failed_read_is_503(store_ok, target):
    store_ok.setattr(*target, _boom)
    await _assert_503(fed_router.list_sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["read_sources", "read_global_settings"])
async def test_health_failed_read_is_503(store_ok, name):
    store_ok.setattr(fed_router.fed_store, name, _boom)
    await _assert_503(fed_router.health)


@pytest.mark.asyncio
async def test_empty_table_still_lists_never_configured_rows(store_ok):
    out = await fed_router.list_sources()
    assert out["global"] == {"enabled": True}
    [row] = out["sources"]
    assert row["source_id"] == "splunk"
    assert (row["enabled"], row["last_poll_at"], row["quiet"]) == (False, None, True)


@pytest.mark.asyncio
async def test_seeded_rows_pass_through(store_ok):
    store_ok.setattr(
        fed_router.fed_store,
        "read_sources",
        lambda: [
            {
                "source_id": "splunk",
                "enabled": True,
                "last_success_at": "2026-10-27T00:00:00",
                "consecutive_errors": 0,
                "last_error": None,
            }
        ],
    )
    store_ok.setattr(
        fed_router,
        "source_collection_lag",
        lambda: [{"source_id": "splunk", "lag_seconds": 5.0, "quiet": False}],
    )
    [row] = (await fed_router.list_sources())["sources"]
    assert (row["enabled"], row["lag_seconds"], row["quiet"]) == (True, 5.0, False)
    health = await fed_router.health()
    assert health["global"] == {"enabled": True}
    assert health["sources"][0]["source_id"] == "splunk"


def test_global_settings_read_raises_when_the_query_fails(monkeypatch):
    # A failed system_config query must not read as "federation is off".
    monkeypatch.setattr("core.storage.config_service.get_session", _boom)
    with pytest.raises(RuntimeError):
        fed_router.fed_store.read_global_settings()
