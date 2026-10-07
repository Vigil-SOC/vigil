#!/usr/bin/env python3
"""Generate the committed snapshot of the frozen tools on the ``vigil`` MCP server.

The snapshot is the promise: for each name in ``tools.mcp.vigil.FROZEN_TOOLS``,
the input schema a caller reads from ``tools/list``. Descriptions are left out
for the reason ``build_contract`` drops them: wording is not the promise.

``tests/unit/api/test_mcp_frozen_tools.py`` rebuilds this and fails if it drifts
from the committed file. To intentionally change a frozen tool, run this script
and commit the diff:

    python scripts/generate_mcp_frozen_tools.py

Imports only ``tools.mcp.vigil``, never the FastAPI app.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tools" / "mcp" / "frozen_tools.snapshot.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_api_v1_contract import serialize  # noqa: E402
from tools.mcp.vigil import FROZEN_TOOLS, mcp  # noqa: E402


def build_snapshot(server: Any, frozen: frozenset[str]) -> dict:
    """Map each frozen tool ``server`` publishes to its input schema."""
    return {
        tool.name: tool.parameters
        for tool in server._tool_manager.list_tools()
        if tool.name in frozen
    }


def main() -> int:
    SNAPSHOT.write_text(serialize(build_snapshot(mcp, FROZEN_TOOLS)))
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
