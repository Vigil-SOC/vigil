"""Live connector checks: each connector run against a real instance.

Opt-in. The tree is skipped unless ``VIGIL_LIVE_CONNECTORS=1``, so ``pytest
tests/`` (CI, the nightly suite, a developer's run) never reaches a vendor. The
weekly ``connectors-live.yml`` workflow sets it.

A probe whose credentials are absent is skipped as not configured rather than
failed: an account nobody has made yet is a gap in the matrix, not a broken
connector. With ``LIVE_CONNECTORS_REPORT`` set, the session writes one result
per probe to that path for the connector matrix.

To run a group locally, do what its workflow job does, e.g.::

    LIVE_ENV_FILE=/tmp/live.env bash tests/live/ci/search_backends.sh up
    set -a; . /tmp/live.env; set +a
    VIGIL_LIVE_CONNECTORS=1 pytest tests/live/test_search_backends.py --no-cov
    bash tests/live/ci/search_backends.sh down
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_results: dict[str, dict] = {}


def pytest_collection_modifyitems(config, items):
    if os.environ.get("VIGIL_LIVE_CONNECTORS") == "1":
        return
    skip = pytest.mark.skip(reason="live connector checks: set VIGIL_LIVE_CONNECTORS=1")
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    probe = getattr(item, "callspec", None) and item.callspec.params.get("probe")
    if probe is None or os.environ.get("VIGIL_LIVE_CONNECTORS") != "1":
        return
    if report.when == "call" or (report.when == "setup" and not report.passed):
        if report.skipped:
            status = "not-configured"
            detail = report.longrepr[2] if isinstance(report.longrepr, tuple) else ""
        elif report.passed:
            status = "pass"
            detail = getattr(item, "_live_detail", "")
        else:
            status = "fail"
            detail = report.longreprtext.strip().splitlines()[-1][:300]
        _results[probe.id] = {
            "server": probe.server,
            "probe": probe.id,
            "tool": probe.tool,
            "status": status,
            "detail": detail.removeprefix("Skipped: "),
            "duration_s": round(report.duration, 1),
            "transport": getattr(item, "_live_transport", None),
        }


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("LIVE_CONNECTORS_REPORT")
    if not path or not _results:
        return
    from tests.live.matrix import connector_version

    for result in _results.values():
        result["version"] = connector_version(result["server"])
    Path(path).write_text(
        json.dumps(
            {
                "commit": _commit(),
                "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "results": sorted(_results.values(), key=lambda r: r["probe"]),
            },
            indent=2,
        )
        + "\n"
    )


@pytest.fixture
async def mcp_client(tmp_path, monkeypatch):
    """A fresh production ``MCPClient`` per probe, closed after it.

    Per probe rather than per session: one server that hangs on shutdown or
    leaves a broken session must not decide the next connector's result.
    An empty ``VIGIL_DIR``, because stored integration config outranks env: a
    developer's saved Splunk URL would otherwise win over the instance under test.
    """
    from core.config import get_settings
    from tests.live.harness import build_client

    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    # Settings hold MCP_RUNNER_URL; a probe fixture may have unset it.
    get_settings.cache_clear()
    client = build_client()
    try:
        yield client
    finally:
        await client.close_all()
