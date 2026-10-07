"""A plain image build keeps the repo VERSION; only an explicit build arg overrides it.

`start.sh restore` / `backup` reject an image whose /app/VERSION is "dev", and a
plain `docker compose build` passes no VIGIL_VERSION. So the Dockerfiles must
default the arg to empty and write /app/VERSION only when it is set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCKER_DIR = Path(__file__).resolve().parents[3] / "infra" / "docker"
DOCKERFILES = ["Dockerfile.backend", "Dockerfile.daemon", "Dockerfile.agent"]


@pytest.mark.parametrize("name", DOCKERFILES)
def test_version_arg_defaults_empty(name: str) -> None:
    text = (DOCKER_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"^ARG VIGIL_VERSION=$", text, re.M), f"{name}: default must be empty"


@pytest.mark.parametrize("name", DOCKERFILES)
def test_version_write_is_conditional(name: str) -> None:
    text = (DOCKER_DIR / name).read_text(encoding="utf-8")
    writes = [line for line in text.splitlines() if "> /app/VERSION" in line]
    assert writes, f"{name}: expected a VERSION write"
    for line in writes:
        assert '[ -n "${VIGIL_VERSION}" ]' in line, f"{name}: unconditional write: {line}"
