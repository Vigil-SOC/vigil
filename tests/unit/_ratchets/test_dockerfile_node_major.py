"""Every Node base image in infra/docker uses one major.

The agent image fell behind on node:20 after it reached end of life while
mcp-runner was already on 22. Moving them together keeps them in step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCKER_DIR = Path(__file__).resolve().parents[3] / "infra" / "docker"
NODE_FROM = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?node:(\d+)", re.M | re.I)


def test_node_base_images_share_one_major() -> None:
    majors: dict[str, set[str]] = {}
    for path in sorted(DOCKER_DIR.glob("Dockerfile*")):
        for major in NODE_FROM.findall(path.read_text(encoding="utf-8")):
            majors.setdefault(major, set()).add(path.name)
    assert majors, "expected at least one FROM node:<major> line"
    assert len(majors) == 1, f"Node base images disagree on major: {majors}"
