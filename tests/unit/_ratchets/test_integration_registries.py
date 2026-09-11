"""The integration registries must agree with each other.

Five registries had drifted apart — ``mcp-config.json`` keys, descriptor ``id``,
descriptor server names, the frontend catalog, and the snake_case key each
vendor server passed to ``get_integration_config``. The damage was silent: six
servers read a key the Settings UI never writes, and every server name in the
bridge's map matched nothing at all.

The descriptor is now the source of truth. These assertions hold it to the two
registries it cannot generate: ``mcp-config.json`` and the frontend catalog.

Follows the ``config.py`` ↔ ``env.example`` precedent in
``test_settings_env_example.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.integrations._base.descriptor import iter_descriptors
from core.integrations.integration_secrets import INTEGRATION_SECRET_FIELDS
from core.integrations.mcp.service import extract_required_env_vars

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MCP_CONFIG = _REPO_ROOT / "mcp-config.json"
_CATALOG = _REPO_ROOT / "clients" / "web" / "src" / "config" / "integrations.ts"
_SETTINGS_DATA = (
    _REPO_ROOT
    / "clients"
    / "web"
    / "src"
    / "screens"
    / "settings"
    / "integrationsData.ts"
)
_DATA_SOURCE_DIALOG = (
    _REPO_ROOT
    / "clients"
    / "web"
    / "src"
    / "screens"
    / "setup"
    / "DataSourceDialog.tsx"
)
_TS_PAIR_RE = re.compile(r"'([A-Za-z0-9_-]+)'\s*:\s*'([A-Za-z0-9_-]+)'")


def _mcp_server_keys() -> set[str]:
    servers = json.loads(_MCP_CONFIG.read_text())["mcpServers"]
    return {k for k, v in servers.items() if not isinstance(v, str)}


def _catalog() -> dict[str, dict[str, str]]:
    """Parse the frontend catalog into ``{id: {field_name: field_type}}``."""
    source = _CATALOG.read_text()
    entries: dict[str, dict[str, str]] = {}
    for block in re.split(r"\n  \{\n", source):
        found = re.search(r"id: *'([A-Za-z0-9_-]+)'", block)
        if not found:
            continue
        fields = re.findall(
            r"name: *'([A-Za-z_0-9]+)',\s*\n\s*label:[^\n]*\n\s*type: *'([a-z]+)'",
            block,
        )
        entries[found.group(1)] = dict(fields)
    return entries


def _descriptors():
    return sorted(iter_descriptors(), key=lambda d: d.id)


def _object_literal(source: str, marker: str) -> str:
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for i, ch in enumerate(source[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace : i + 1]
    raise AssertionError(f"unclosed object literal after {marker!r}")


def _ts_string_map(path: Path, marker: str) -> dict[str, str]:
    return dict(_TS_PAIR_RE.findall(_object_literal(path.read_text(), marker)))


def _hidden_mcp_servers() -> set[str]:
    source = _SETTINGS_DATA.read_text()
    found = re.search(r"HIDDEN_MCP_SERVERS = new Set\(\[([^\]]+)\]\)", source)
    assert found, "HIDDEN_MCP_SERVERS not found in integrationsData.ts"
    return set(re.findall(r"'([A-Za-z0-9_-]+)'", found.group(1)))


@pytest.mark.unit
def test_every_server_name_resolves_to_a_real_mcp_config_key():
    keys = _mcp_server_keys()
    dangling = {
        d.id: [name for name in d.mcp_server_names if name not in keys]
        for d in _descriptors()
        if any(name not in keys for name in d.mcp_server_names)
    }
    assert not dangling, (
        "descriptors naming MCP servers that do not exist in mcp-config.json: "
        f"{dangling}"
    )


@pytest.mark.unit
def test_every_descriptor_id_is_a_real_catalog_entry():
    catalog = _catalog()
    orphans = [d.id for d in _descriptors() if d.id not in catalog]
    assert not orphans, (
        "descriptors with no row in clients/web/src/config/integrations.ts, so "
        f"they can never be configured in the UI: {orphans}"
    )


@pytest.mark.unit
def test_descriptor_fields_match_the_catalog_form():
    """A field a server reads must be one the Settings form actually collects.

    Carbon Black read ``api_token`` while the form collected ``api_id`` and
    ``api_key``; Palo Alto read ``url`` while the form collected ``hostname``.
    Both resolved to None forever.
    """
    catalog = _catalog()
    mismatched = {}
    for descriptor in _descriptors():
        expected = set(catalog.get(descriptor.id, {}))
        declared = set(descriptor.field_names)
        if declared != expected:
            mismatched[descriptor.id] = {
                "declared_not_in_catalog": sorted(declared - expected),
                "in_catalog_not_declared": sorted(expected - declared),
            }
    assert not mismatched, f"descriptor/catalog field drift: {mismatched}"


@pytest.mark.unit
def test_every_catalog_password_field_is_stored_as_a_secret():
    """One-way: a password field must be secret, but a descriptor may protect
    more than the form marks (vstrike's username is read via get_secret)."""
    catalog = _catalog()
    unprotected = {}
    for integration_id, fields in catalog.items():
        passwords = {name for name, kind in fields.items() if kind == "password"}
        registered = set(INTEGRATION_SECRET_FIELDS.get(integration_id, {}))
        leaked = passwords - registered
        if leaked:
            unprotected[integration_id] = sorted(leaked)
    assert not unprotected, (
        "password fields that would be persisted in plaintext because nothing "
        f"registers them as secrets: {unprotected}"
    )


@pytest.mark.unit
def test_no_vendor_server_lives_in_the_tools_package():
    """``tools/`` holds only servers that talk to Vigil's own services."""
    strays = sorted(
        p.name for p in (_REPO_ROOT / "tools").glob("*.py") if p.name != "__init__.py"
    )
    assert not strays, (
        "vendor MCP servers must live in core/integrations/<vendor>/tool.py, "
        f"not tools/: {strays}"
    )


@pytest.mark.unit
def test_frontend_server_catalog_maps_are_inverses():
    """Settings and setup keep hand-written inverses of the same aliases.

    ``SERVER_TO_INTEGRATION`` (server key → catalog id) and
    ``CATALOG_TO_SERVER`` (catalog id → server key) live in different files.
    Elastic dropped out of one and the Settings card lost its gear.
    """
    server_to = _ts_string_map(_SETTINGS_DATA, "SERVER_TO_INTEGRATION")
    catalog_to = _ts_string_map(_DATA_SOURCE_DIALOG, "CATALOG_TO_SERVER")
    assert {v: k for k, v in server_to.items()} == catalog_to, (
        "SERVER_TO_INTEGRATION and CATALOG_TO_SERVER are not inverses: "
        f"{server_to!r} vs {catalog_to!r}"
    )


@pytest.mark.unit
def test_aliased_mcp_server_names_are_in_the_frontend_maps():
    """A descriptor whose MCP key differs from its catalog id must be mapped.

    Hidden servers (``splunk-selfhosted``) are not Settings cards, so they
    are not required in the 1:1 alias maps.
    """
    server_to = _ts_string_map(_SETTINGS_DATA, "SERVER_TO_INTEGRATION")
    catalog_to = _ts_string_map(_DATA_SOURCE_DIALOG, "CATALOG_TO_SERVER")
    hidden = _hidden_mcp_servers()
    missing = {}
    for descriptor in _descriptors():
        for name in descriptor.mcp_server_names:
            if name == descriptor.id or name in hidden:
                continue
            problems = []
            if server_to.get(name) != descriptor.id:
                problems.append(
                    f"SERVER_TO_INTEGRATION[{name!r}] is {server_to.get(name)!r}, "
                    f"expected {descriptor.id!r}"
                )
            if catalog_to.get(descriptor.id) != name:
                problems.append(
                    f"CATALOG_TO_SERVER[{descriptor.id!r}] is "
                    f"{catalog_to.get(descriptor.id)!r}, expected {name!r}"
                )
            if problems:
                missing[f"{descriptor.id}/{name}"] = problems
    assert not missing, (
        "descriptor mcp_server_names that differ from id must appear in both "
        f"frontend maps:\n{missing}"
    )


@pytest.mark.unit
def test_elastic_mcp_config_declares_no_required_env_placeholders():
    """Settings never writes ELASTIC_HOST; a ${ELASTIC_HOST} placeholder
    marks the server dormant even after a UI save."""
    servers = json.loads(_MCP_CONFIG.read_text())["mcpServers"]
    elastic = servers["elastic"]
    env = {
        k: str(v)
        for k, v in (elastic.get("env") or {}).items()
        if not k.startswith("_")
    }
    assert extract_required_env_vars(env, list(elastic.get("args") or [])) == []


@pytest.mark.unit
def test_atomic_red_team_mcp_config_declares_no_required_env_placeholders():
    """runner_path is a non-secret Settings field. A ${ATOMIC_RED_TEAM_RUNNER_PATH}
    placeholder would keep the server dormant after a UI save, because
    split_secrets never writes that name."""
    servers = json.loads(_MCP_CONFIG.read_text())["mcpServers"]
    art = servers["atomic-red-team"]
    env = {
        k: str(v)
        for k, v in (art.get("env") or {}).items()
        if not k.startswith("_")
    }
    assert extract_required_env_vars(env, list(art.get("args") or [])) == []
