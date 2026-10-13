"""Resolve a Vendor Slice's configuration from its descriptor.

The one way an MCP server reads its own config. Non-secret fields come from the
stored integration config; secret fields come from the encrypted store, keyed by
the same names ``integration_secrets`` writes them under.

Reading a credential straight from ``get_integration_config`` cannot work:
``split_secrets`` strips every registered secret field before anything is
persisted, so the credential is never in the JSON or the DB. Several servers did
exactly that and silently ran with ``None`` — this module exists so no server has
to know the rule.

``resolve`` is the descriptor-driven entry point. ``resolve_fields`` takes an
explicit field list for callers with no descriptor, such as a generated custom
integration. ``is_configured`` is the one "is it set up" answer built on it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from core.config import get_integration_config
from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    iter_descriptors,
)
from core.integrations.integration_secrets import env_var_for, secret_fields_for
from core.secrets import get_secret

logger = logging.getLogger(__name__)


def _resolve(
    integration_id: str, fields: Iterable[IntegrationField]
) -> Dict[str, Optional[Any]]:
    stored: Mapping[str, Any] = get_integration_config(integration_id) or {}
    secret_keys = secret_fields_for(integration_id)

    resolved: Dict[str, Optional[Any]] = {}
    for field in fields:
        env_key = secret_keys.get(field.name)
        if env_key is not None:
            value = get_secret(env_key)
        else:
            value = stored.get(field.name)
            if value is None or value == "":
                value = get_secret(env_var_for(integration_id, field.name))
        resolved[field.name] = field.coerce(value)
    return resolved


def resolve_fields(
    integration_id: str, field_names: Iterable[str]
) -> Dict[str, Optional[Any]]:
    """Resolve named fields for an integration, secrets included.

    A non-secret falls back to ``get_secret`` when the stored config has no
    value: that is the channel an ``mcp-config.json`` ``env`` block feeds, and
    it keeps env-configured servers working without a raw environment read.

    Values come back as stored, uncoerced: there is no descriptor here to say
    what type each field should be. A caller that has one should use
    ``resolve``.
    """
    return _resolve(integration_id, (IntegrationField(n) for n in field_names))


def resolve(descriptor: IntegrationDescriptor) -> Dict[str, Optional[Any]]:
    """Resolve every field the descriptor declares, typed as it declares it."""
    return _resolve(descriptor.id, descriptor.fields)


def missing(config: Mapping[str, Any], *required: str) -> tuple[str, ...]:
    """Which of ``required`` resolved to nothing — the 'not configured' check."""
    return tuple(name for name in required if not config.get(name))


# How long an is_configured answer stands. The MCP registry asks on every read of
# its tool list, several times a chat turn and inside async handlers, and a fresh
# answer is a database read. A save in Settings drops it (forget_configured).
CONFIGURED_TTL_S = 15.0

# descriptor id -> (time.monotonic() when answered, answer)
_answers: Dict[str, Tuple[float, bool]] = {}

# Bumped by forget_configured. A read that started before a save keeps its answer
# to itself rather than writing it back over the save.
_generation = 0


def is_configured(descriptor: IntegrationDescriptor) -> bool:
    """Whether every field the descriptor marks required has a value.

    Saved in Settings or set in the env: what ``resolve`` reads, so what the
    server itself will read. Not whether the values work; ``credential_check``
    logs in. A descriptor that marks nothing required needs nothing saved.
    """
    required = descriptor.required_fields
    if not required:
        return True
    now = time.monotonic()
    last = _answers.get(descriptor.id)
    if last is not None and now - last[0] < CONFIGURED_TTL_S:
        return last[1]
    generation = _generation
    try:
        answer = not missing(resolve(descriptor), *required)
    except Exception as exc:  # noqa: BLE001
        # The class name only, as in the servers: a decrypt error can carry the
        # value it choked on. A read that failed says nothing new about what is
        # saved, so the last answer stands; with none, the server reads through
        # the same resolve() and cannot read its settings either.
        logger.warning(
            "%s config could not be resolved (%s)", descriptor.id, type(exc).__name__
        )
        answer = last[1] if last is not None else False
    if generation == _generation:
        _answers[descriptor.id] = (now, answer)
    return answer


def forget_configured() -> None:
    """Drop every cached ``is_configured`` answer, so this process reads a save
    at once. Other processes see it when their answer expires."""
    global _generation
    _generation += 1
    _answers.clear()


def server_configured(server_name: str) -> bool:
    """``is_configured`` for the integration behind an MCP server.

    True for a server no descriptor names: nothing says what it needs.
    """
    for descriptor in iter_descriptors():
        if server_name in descriptor.mcp_server_names:
            return is_configured(descriptor)
    return True
