"""Unit tests for the routability predicate in core.llm.bifrost.mirror.

Bifrost >= 1.6 reports a credential as ``{"value", "ref", "type"}``; the old
shape was ``{"env_var", "from_env"}``. The guard must recognise an unresolved
reference in either shape and must NOT mistake a resolved env credential
(masked, non-empty value) for a placeholder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.llm.bifrost.mirror import _cred_from_env, key_is_routable  # noqa: E402

pytestmark = pytest.mark.unit

UNRESOLVED_ENV = {"value": "", "ref": "env.ANTHROPIC_API_KEY", "type": "env"}
RESOLVED_ENV = {"value": "sk-***abcd", "ref": "env.ANTHROPIC_API_KEY", "type": "env"}
LITERAL = {"value": "sk-***abcd", "ref": "", "type": ""}


@pytest.mark.parametrize(
    "cred, expected",
    [
        (UNRESOLVED_ENV, True),
        (RESOLVED_ENV, False),
        (LITERAL, False),
        ({"value": "", "ref": "vault.secret/x", "type": "vault"}, True),
        ({"value": "", "ref": "env.X"}, True),  # ref only, no type
        ({"env_var": "ANTHROPIC_API_KEY", "from_env": True}, True),  # legacy
        # legacy marker but resolved: same "empty value" rule as the new shape
        ({"env_var": "ANTHROPIC_API_KEY", "from_env": True, "value": "sk-***"}, False),
        ({"value": ""}, False),  # empty but not a reference: not our call
    ],
)
def test_cred_from_env_value_shapes(cred, expected):
    assert _cred_from_env({"value": cred}) is expected


def test_cred_from_env_vertex_auth_credentials():
    key = {
        "value": RESOLVED_ENV,
        "vertex_key_config": {
            "auth_credentials": {
                "value": "",
                "ref": "env.GOOGLE_APPLICATION_CREDENTIALS",
                "type": "env",
            }
        },
    }
    assert _cred_from_env(key) is True
    key["vertex_key_config"]["auth_credentials"]["value"] = "{***}"
    assert _cred_from_env(key) is False


def test_string_value_is_not_a_reference():
    assert _cred_from_env({"value": "sk-literal"}) is False


def test_unresolved_seed_is_not_routable_without_status():
    assert key_is_routable({"value": UNRESOLVED_ENV}, "anthropic") is False
    assert key_is_routable({"value": UNRESOLVED_ENV, "status": "unknown"}) is False


def test_resolved_and_literal_keys_are_routable_without_status():
    assert key_is_routable({"value": RESOLVED_ENV}, "anthropic") is True
    assert key_is_routable({"value": LITERAL}, "openai") is True


def test_unverifiable_vertex_with_unresolved_creds_is_not_routable():
    key = {
        "value": UNRESOLVED_ENV,
        "status": "list_models_failed",
        "vertex_key_config": {
            "auth_credentials": {"value": "", "ref": "env.GAC", "type": "env"}
        },
    }
    assert key_is_routable(key, "vertex") is False
    key["value"] = RESOLVED_ENV
    key["vertex_key_config"]["auth_credentials"]["value"] = "{***}"
    assert key_is_routable(key, "vertex") is True


@pytest.mark.parametrize(
    "key",
    [
        {"value": UNRESOLVED_ENV},
        {"value": {"env_var": "X", "from_env": True}},
        {"ollama_key_config": {"url": RESOLVED_ENV}},
    ],
)
def test_success_status_is_routable_regardless_of_shape(key):
    assert key_is_routable({**key, "status": "success"}) is True


def test_disabled_key_is_never_routable():
    assert (
        key_is_routable({"value": LITERAL, "status": "success", "enabled": False})
        is False
    )
