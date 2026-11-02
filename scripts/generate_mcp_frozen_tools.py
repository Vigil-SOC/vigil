#!/usr/bin/env python3
"""Generate the committed snapshot of the frozen tools on the ``vigil`` MCP server.

The snapshot is the promise: for each name in ``tools.mcp.vigil.FROZEN_TOOLS``,
the input schema a caller reads from ``tools/list``. Descriptions are left out
for the reason ``build_contract`` drops them: wording is not the promise.

To intentionally change a frozen tool, run this script and commit the diff:

    python scripts/generate_mcp_frozen_tools.py

Imports only ``tools.mcp.vigil``, never the FastAPI app, under a pinned
environment (``DEV_MODE=false``, a placeholder ``JWT_SECRET_KEY`` if none is
set, ``VIGIL_DISABLE_DOTENV=1``) so it runs on a fresh checkout and writes the
same snapshot on every machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tools" / "mcp" / "frozen_tools.snapshot.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importing tools.mcp.vigil loads core.auth.auth_service, which refuses to import
# without JWT_SECRET_KEY unless DEV_MODE is on. Pin the environment, so this
# writes the same snapshot on every machine:
#
#   DEV_MODE — assigned "false", not defaulted: the snapshot is built with it off.
#   JWT_SECRET_KEY — `or`, not setdefault: a shell that sourced env.example
#     exports "". The placeholder only satisfies the import; nothing is signed.
#   VIGIL_DISABLE_DOTENV — a developer's .env must not leak into the snapshot.
os.environ["DEV_MODE"] = "false"
os.environ["JWT_SECRET_KEY"] = (
    os.environ.get("JWT_SECRET_KEY") or "snapshot-only-secret-not-for-prod"
)
os.environ["VIGIL_DISABLE_DOTENV"] = "1"

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
