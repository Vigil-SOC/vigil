import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Both trees: the vendor MCP servers this guard was written for now live in
# core/integrations/<vendor>/tool.py, and scanning only tools/ would pass
# vacuously.
SCANNED_DIRS = (_REPO_ROOT / "tools", _REPO_ROOT / "core" / "integrations")


def _verify_false_lines(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "verify" and getattr(kw.value, "value", None) is False:
                yield kw.value.lineno


@pytest.mark.unit
def test_no_hardcoded_tls_verify_disabled_in_tools():
    violations = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}"
        for directory in SCANNED_DIRS
        for path in directory.rglob("*.py")
        for lineno in _verify_false_lines(path)
    ]
    assert not violations, (
        "Hardcoded 'verify=False' disables TLS verification unconditionally. "
        "Use verify=config.get('verify_ssl', True) so operators opt out "
        "explicitly.\n" + "\n".join(violations)
    )


def _verify_ssl_false_defaults(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        positional = args.posonlyargs + args.args
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d]
        for arg, default in pairs:
            if arg.arg == "verify_ssl" and getattr(default, "value", None) is False:
                yield default.lineno


@pytest.mark.unit
def test_no_verify_ssl_parameter_defaults_to_off():
    """A False default skips the check for every caller that never says."""
    violations = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}"
        for directory in SCANNED_DIRS
        for path in directory.rglob("*.py")
        for lineno in _verify_ssl_false_defaults(path)
    ]
    assert not violations, (
        "verify_ssl must default to True so operators opt out explicitly.\n"
        + "\n".join(violations)
    )


@pytest.mark.unit
def test_the_scan_actually_reaches_the_vendor_servers():
    """The guard above is only as good as its search path.

    It passed while scanning a tools/ that no longer holds a vendor server —
    green, and guarding nothing. Assert the scan reaches real files.
    """
    scanned = [p for d in SCANNED_DIRS if d.exists() for p in d.rglob("*.py")]
    assert len(scanned) > 20, f"scan path covers almost nothing: {len(scanned)} files"

    vendor_tools = [p for p in scanned if p.name == "tool.py"]
    assert vendor_tools, (
        "no <vendor>/tool.py in the scan path — the vendor MCP servers moved "
        "again and this ratchet is now vacuous"
    )
