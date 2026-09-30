"""Regression: Integration IDs for cloud SIEMs must be hyphenated (#555).

Settings persist hyphenated ids (``azure-sentinel``, etc.). Enablement and
config lookups are exact-match, so underscore forms silently never match.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit

CANONICAL_IDS = (
    "azure-sentinel",
    "aws-security-hub",
    "microsoft-defender",
)
LEGACY_UNDERSCORE_IDS = (
    "azure_sentinel",
    "aws_security_hub",
    "microsoft_defender",
)

# Call sites that must use Integration IDs (settings keys), not federation
# source names / Redis namespaces / finding source labels.
TARGET_FILES = (
    "core/integrations/azure_sentinel/ingestion.py",
    "core/integrations/aws_security_hub/ingestion.py",
    "core/integrations/microsoft_defender/ingestion.py",
    "core/integrations/microsoft_defender/tool.py",
    "core/integrations/azure_sentinel/adapter.py",
    "core/integrations/aws_security_hub/adapter.py",
    "core/integrations/microsoft_defender/adapter.py",
)

FEDERATION_ADAPTER_FILES = (
    "core/integrations/azure_sentinel/adapter.py",
    "core/integrations/aws_security_hub/adapter.py",
    "core/integrations/microsoft_defender/adapter.py",
)

INGESTION_SOURCE_FILES = (
    ("core/integrations/azure_sentinel/ingestion.py", "azure_sentinel"),
    ("core/integrations/aws_security_hub/ingestion.py", "aws_security_hub"),
    ("core/integrations/microsoft_defender/ingestion.py", "microsoft_defender"),
)

LOOKUP_FUNCS = frozenset({"is_integration_enabled", "get_integration_config"})


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _string_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _integration_id_literals(path: Path) -> list[tuple[int, str, str]]:
    """Return (lineno, kind, value) for Integration ID string literals."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node.func)
            if name in LOOKUP_FUNCS and node.args:
                value = _string_const(node.args[0])
                if value is not None:
                    found.append((node.lineno, name, value))
            if name == "SIEMIngestionAdapter":
                for kw in node.keywords:
                    if kw.arg == "integration_id":
                        value = _string_const(kw.value)
                        if value is not None:
                            found.append((node.lineno, "integration_id=", value))
    return found


@pytest.mark.parametrize("rel_path", TARGET_FILES)
def test_no_legacy_underscore_integration_ids(rel_path: str):
    literals = _integration_id_literals(REPO_ROOT / rel_path)
    bad = [
        (lineno, kind, value)
        for lineno, kind, value in literals
        if value in LEGACY_UNDERSCORE_IDS
    ]
    assert bad == [], (
        f"{rel_path} still uses underscore Integration IDs "
        f"(expected hyphenated canonical ids): {bad}"
    )


def test_canonical_hyphenated_ids_present_at_call_sites():
    seen: set[str] = set()
    for rel_path in TARGET_FILES:
        for _lineno, _kind, value in _integration_id_literals(REPO_ROOT / rel_path):
            if value in CANONICAL_IDS:
                seen.add(value)
    assert seen == set(CANONICAL_IDS)


@pytest.mark.parametrize("rel_path", FEDERATION_ADAPTER_FILES)
def test_federation_source_names_remain_underscore(rel_path: str):
    """Federation runtime names stay underscore; only integration_id is hyphenated."""
    tree = ast.parse(
        (REPO_ROOT / rel_path).read_text(encoding="utf-8"), filename=rel_path
    )
    names: list[str] = []
    register_keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        if callee == "SIEMIngestionAdapter":
            for kw in node.keywords:
                if kw.arg == "name":
                    value = _string_const(kw.value)
                    if value is not None:
                        names.append(value)
        if callee == "register_adapter" and node.args:
            value = _string_const(node.args[0])
            if value is not None:
                register_keys.append(value)

    assert names, f"{rel_path}: missing SIEMIngestionAdapter name="
    assert register_keys, f"{rel_path}: missing register_adapter key"
    assert all(n in LEGACY_UNDERSCORE_IDS for n in names), names
    assert all(k in LEGACY_UNDERSCORE_IDS for k in register_keys), register_keys


@pytest.mark.parametrize("rel_path,expected_source", INGESTION_SOURCE_FILES)
def test_finding_data_source_values_remain_underscore(
    rel_path: str, expected_source: str
):
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert f'"data_source": "{expected_source}"' in source
