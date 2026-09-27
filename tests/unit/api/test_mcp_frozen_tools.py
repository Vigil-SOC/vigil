"""The frozen tools on the ``vigil`` MCP server may not drift silently.

`tools/mcp/frozen_tools.snapshot.json` is the committed promise: the name and
input schema of every tool in `FROZEN_TOOLS`. This test rebuilds it from the
live server and fails if it differs. If the change is intended, regenerate and
commit the diff:

    python scripts/generate_mcp_frozen_tools.py

Tools outside the set are served under the 0.x terms, so renaming one of them
leaves this test green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit

from scripts.generate_api_v1_contract import serialize  # noqa: E402
from scripts.generate_mcp_frozen_tools import SNAPSHOT, build_snapshot  # noqa: E402
from tools.mcp.vigil import FROZEN_TOOLS, mcp  # noqa: E402


def test_frozen_tools_match_committed_snapshot():
    committed = json.loads(SNAPSHOT.read_text())
    assert build_snapshot(mcp, FROZEN_TOOLS) == committed, (
        f"A frozen MCP tool drifted from {SNAPSHOT.relative_to(REPO)}.\n"
        "If this change is intended, run:\n"
        "    python scripts/generate_mcp_frozen_tools.py\n"
        "and commit the diff. If it is not, a frozen tool's name or input "
        "schema changed by accident — revert it."
    )


def test_snapshot_is_serialized_canonically():
    committed_text = SNAPSHOT.read_text()
    assert committed_text == serialize(json.loads(committed_text))


def test_every_frozen_name_is_published():
    # A name no tool carries is a promise about nothing; it also hides a rename
    # from the snapshot comparison, which only sees tools that exist.
    published = {tool.name for tool in mcp._tool_manager.list_tools()}
    missing = sorted(FROZEN_TOOLS - published)
    assert not missing, f"FROZEN_TOOLS names no published tool carries: {missing}"


def test_snapshot_holds_exactly_the_frozen_set():
    assert set(json.loads(SNAPSHOT.read_text())) == FROZEN_TOOLS
