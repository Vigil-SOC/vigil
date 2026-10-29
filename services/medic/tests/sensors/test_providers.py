"""llm_providers (D6-5), read through the gateway."""

from __future__ import annotations

import json
from typing import Any

import httpx

from services.medic.sensors.base import SensorContext
from services.medic.sensors.gateway import assert_path_subset
from services.medic.sensors.providers import CALLS, LLMProviders
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="compose")
CANARY = "canaryLlmProv2071 sk-hunter2prov"
KEYS = {
    "providers.is_default",
    "providers.is_active",
    "providers.has_api_key",
    "providers.provider_type",
}


def _row(provider_id: str, **over: Any) -> dict[str, Any]:
    row = {
        "provider_id": provider_id,
        "provider_type": "anthropic",
        "name": f"Prod key {CANARY}",
        "base_url": f"https://{CANARY}.example/v1",
        "default_model": "claude-x",
        "config": {"token": CANARY},
        "is_active": True,
        "is_default": True,
        "has_api_key": True,
        "last_test_at": None,
        "last_test_success": None,
        "last_error": CANARY,
        "surprise": CANARY,
        "created_at": "2026-10-01T08:00:00+00:00",
        "updated_at": "2026-10-07T09:59:58+00:00",
    }
    return row | over


class Backend:
    """Stands in for the gateway; `body` can change between ticks."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status, self.body, self.paths = status, body, []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            return httpx.Response(
                self.status,
                content=json.dumps(self.body).encode(),
                headers={"content-type": "application/json"},
            )

        return httpx.MockTransport(handler)


def _sensor(backend: Backend) -> LLMProviders:
    return LLMProviders("medic-gateway-out", 8471, transport=backend.transport())


def _values(obs: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
    out = {}
    for o in obs:
        if o["kind"] == "sample" and o["signal"] == "llm_providers":
            for v in o["values"]:
                out[(v["key"], v["labels"]["provider"])] = v["value"]
    return out


async def _read(*rows: dict[str, Any]) -> dict[tuple[str, str], Any]:
    rig = Rig([_sensor(Backend(body=list(rows)))])
    got = _values(await rig.tick())
    rig.assert_all_valid()
    return got


def test_the_one_call_is_on_outbound() -> None:
    assert_path_subset(CALLS)


async def test_a_keyed_active_default_emits_the_rule_keys() -> None:
    backend = Backend(body=[_row("anthropic-prod")])
    rig = Rig([_sensor(backend)])
    got = _values(await rig.tick())
    rig.assert_all_valid()
    assert backend.paths == ["/api/llm/providers"]
    assert {k for k, _ in got} == KEYS
    assert got == {
        ("providers.is_default", "anthropic-prod"): True,
        ("providers.is_active", "anthropic-prod"): True,
        ("providers.has_api_key", "anthropic-prod"): True,
        ("providers.provider_type", "anthropic-prod"): "anthropic",
    }


async def test_no_default_provider() -> None:
    got = await _read(_row("a", is_default=False), _row("b", is_default=False))
    assert got[("providers.is_default", "a")] is False
    assert got[("providers.is_default", "b")] is False


async def test_an_inactive_default() -> None:
    got = await _read(_row("a", is_active=False))
    assert got[("providers.is_default", "a")] is True
    assert got[("providers.is_active", "a")] is False


async def test_a_default_without_a_key() -> None:
    got = await _read(_row("a", has_api_key=False))
    assert got[("providers.has_api_key", "a")] is False


async def test_a_local_only_provider_is_read_like_any_other() -> None:
    got = await _read(_row("local", provider_type="ollama", has_api_key=False))
    assert got[("providers.provider_type", "local")] == "ollama"
    assert got[("providers.has_api_key", "local")] is False
    assert got[("providers.is_active", "local")] is True


async def test_a_non_bool_flag_is_absent_not_coerced() -> None:
    rig = Rig([_sensor(Backend(body=[_row("a", is_active="yes", has_api_key=1)]))])
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    states = {v["key"]: v["state"] for v in sample["values"]}
    assert states["providers.is_active"] == "absent"
    assert states["providers.has_api_key"] == "absent"


async def test_an_empty_list_is_a_result_with_no_rows() -> None:
    rig = Rig([_sensor(Backend(body=[]))])
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    assert sample["outcome"] == "ok" and sample["values"] == []


async def test_a_relayed_500_is_a_result_with_no_values() -> None:
    rig = Rig([_sensor(Backend(500, {"detail": "Internal Server Error"}))])
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    assert sample["outcome"] == "ok" and sample["values"] == []


async def test_a_200_of_another_shape_is_a_parse_error() -> None:
    for body in (
        {},
        {"providers": []},
        ["x"],
        [{"is_default": True}],
        [{"provider_id": ""}],
    ):
        (reading,) = await _sensor(Backend(body=body)).collect(CTX)
        assert reading.error is not None and reading.error.cls == "parse"
        assert reading.values == ()


async def test_an_unreachable_gateway_errors_then_reads_blind() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused") from ConnectionRefusedError()

    transport = httpx.MockTransport(refuse)
    rig = Rig([LLMProviders("medic-gateway-out", 8471, transport=transport)])
    obs = await rig.tick()
    (sample,) = [o for o in obs if o["kind"] == "sample"]
    assert sample["outcome"] == "error" and sample["error"]["class"] == "refused"
    for _ in range(4):
        await rig.tick(240)
    rig.assert_all_valid()
    assert rig.health("llm_providers")[-1]["state"] == "blind"


async def test_no_free_text_field_leaves_the_sensor() -> None:
    rig = Rig(
        [_sensor(Backend(body=[_row("a"), _row("b", is_default=False)]))], choke=None
    )
    await rig.tick()
    rig.assert_all_valid()
    dumped = json.dumps(rig.sink.observations)
    assert "canaryLlmProv2071" not in dumped and "hunter2prov" not in dumped
