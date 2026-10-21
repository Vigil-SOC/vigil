"""Bifrost logs request metadata for costs, never prompt or response bodies.

Issue #2572: Vigil reads only cost, token and latency metadata from Bifrost's
log (``core.llm.bifrost.costs``), so keeping every prompt and response in
``logs.db`` was a second copy of sensitive data nothing used. Bifrost re-syncs
the ``client`` block from this file on boot, so existing installs follow it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Also the desktop's bifrost-config.json (copied by prepare-standalone.js).
BIFROST_CONFIG = Path(__file__).resolve().parents[3] / "infra" / "docker" / "bifrost" / "config.json"


def test_bifrost_logs_metadata_without_content() -> None:
    cfg = json.loads(BIFROST_CONFIG.read_text(encoding="utf-8"))
    assert cfg["client"]["disable_content_logging"] is True, "Bifrost must not store request/response bodies"
    # The cost views, budgets and recalculate-cost read this log's metadata.
    assert cfg["client"]["enable_logging"] is True
    assert cfg["logs_store"]["enabled"] is True
