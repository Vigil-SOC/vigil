"""llm_providers (D6-5): per-provider state from `/api/llm/providers`.

Read through the gateway (`gateway.get`). Emits only the keys a rule reads:

| Gateway answered                          | Reading                                |
|-------------------------------------------|----------------------------------------|
| 200 `[{provider_id, ...}, ...]`           | per-provider values (label `provider`) |
| a relayed backend 5xx (store read failed) | no values (a result)                   |
| 200 with any other body                   | error `parse`                          |
| anything `classify` calls an error        | that error                             |

Only `provider_id`, `provider_type`, `is_default`, `is_active` and `has_api_key`
are read: `name`, `base_url`, `config` and `last_error` are operator or remote
text. `has_api_key` means a secret reference is stored, not that the key works.
Every provider type is treated the same.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value
from services.medic.sensors.gateway import GatewayCall, get

SIGNAL = "llm_providers"
CALLS = (GatewayCall("/api/llm/providers"),)


def _flag(raw: object) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _provider(row: Mapping[str, Any], provider_id: str) -> list[Value]:
    labels = {"provider": provider_id}
    kind = row.get("provider_type")
    return [
        Value.flag("providers.is_default", _flag(row.get("is_default")), labels),
        Value.flag("providers.is_active", _flag(row.get("is_active")), labels),
        Value.flag("providers.has_api_key", _flag(row.get("has_api_key")), labels),
        Value.enum(
            "providers.provider_type",
            kind if isinstance(kind, str) and kind else None,
            labels,
        ),
    ]


def _parse(body: Any) -> list[Value] | None:
    """None when the body isn't a list of `{provider_id, ...}`."""
    if not isinstance(body, list):
        return None
    values: list[Value] = []
    for row in body:
        if not isinstance(row, dict):
            return None
        provider_id = row.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return None
        values += _provider(row, provider_id)
    return values


@dataclass
class LLMProviders:
    host: str
    port: int
    id: str = SIGNAL
    service: str = "backend"
    interval_s: int = 60  # epic decision 7
    timeout_s: float = 10.0
    covers: tuple[str, ...] = (SIGNAL,)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        got = await get(
            self.host,
            self.port,
            CALLS[0].path,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [Reading(SIGNAL, "backend", error=got)]
        if got.status >= 500:
            # The backend answered that it couldn't read its store: no rows, not an error.
            values: list[Value] = []
        elif (parsed := _parse(got.body)) is None:
            return [Reading(SIGNAL, "backend", error=ReadError("parse"))]
        else:
            values = parsed
        return [Reading(SIGNAL, "backend", values=values)]


def llm_providers(host: str, port: int) -> LLMProviders:
    return LLMProviders(host, port)
