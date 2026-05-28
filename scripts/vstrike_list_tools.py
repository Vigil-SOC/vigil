"""VStrike MCP catalog probe.

Calls `tools/list` against the live VStrike MCP endpoint and diffs the
returned catalog against the tool names we actually invoke via
`services/vstrike_service.py:_call_mcp_tool(...)`. Anything upstream
exposes that we don't call ends up in the `MISSING WRAPPERS:` section.

Usage::

    VSTRIKE_BASE_URL=https://vstrike.example.com \\
    VSTRIKE_USERNAME=... \\
    VSTRIKE_PASSWORD=... \\
    python scripts/vstrike_list_tools.py

Exit code 0 → catalog fetched (whether or not wrappers are missing).
Non-zero → transport / auth / parse failure.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.vstrike_service import VStrikeService  # noqa: E402


SERVICE_FILE = REPO_ROOT / "services" / "vstrike_service.py"
CALL_PATTERN = re.compile(r'_call_mcp_tool\(\s*"([a-z][a-z0-9\-]*)"')


def wrapped_tool_names() -> Set[str]:
    """Tool names we actually call from VStrikeService."""
    text = SERVICE_FILE.read_text(encoding="utf-8")
    return set(CALL_PATTERN.findall(text))


def _truthy(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _required_fields(schema: Any) -> List[str]:
    if isinstance(schema, dict):
        req = schema.get("required")
        if isinstance(req, list):
            return [str(x) for x in req]
    return []


def main() -> int:
    base_url = os.getenv("VSTRIKE_BASE_URL")
    username = os.getenv("VSTRIKE_USERNAME")
    password = os.getenv("VSTRIKE_PASSWORD")
    verify_ssl = _truthy(os.getenv("VSTRIKE_VERIFY_SSL", "true"))

    if not base_url or not username or not password:
        print(
            "VSTRIKE_BASE_URL, VSTRIKE_USERNAME, and VSTRIKE_PASSWORD "
            "must be set in the environment.",
            file=sys.stderr,
        )
        return 2

    svc = VStrikeService(
        base_url=base_url,
        verify_ssl=verify_ssl,
        username=username,
        password=password,
    )

    try:
        tools: List[Dict[str, Any]] = svc.list_tools()
    except RuntimeError as e:
        print(f"tools/list failed: {e}", file=sys.stderr)
        return 1

    upstream_names: Set[str] = set()
    print(f"=== VStrike MCP catalog ({len(tools)} tools) ===\n")
    for tool in sorted(tools, key=lambda t: str(t.get("name") or "")):
        name = str(tool.get("name") or "<unnamed>")
        upstream_names.add(name)
        desc = str(tool.get("description") or "").strip().splitlines()
        first_line = desc[0] if desc else ""
        required = _required_fields(tool.get("inputSchema"))
        suffix = f"  [required: {', '.join(required)}]" if required else ""
        print(f"  {name}{suffix}")
        if first_line:
            print(f"    {first_line}")

    wrapped = wrapped_tool_names()
    missing = sorted(upstream_names - wrapped)
    extra = sorted(wrapped - upstream_names)

    print("\n=== MISSING WRAPPERS (upstream exposes, we don't call) ===")
    if not missing:
        print("  (none — every upstream tool has a wrapper)")
    else:
        for name in missing:
            marker = "  ⚠ " if "network-graph" in name else "  - "
            print(f"{marker}{name}")

    if extra:
        print("\n=== STALE CALLS (we call, upstream doesn't expose) ===")
        for name in extra:
            print(f"  - {name}")

    print(
        f"\nSummary: upstream={len(upstream_names)} wrapped={len(wrapped)} "
        f"missing={len(missing)} stale={len(extra)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
