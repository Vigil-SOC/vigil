"""Compose persists the State Directory and investigation workdirs (#1263).

Without a named volume, master.key lives in the container's writable layer:
`--force-recreate` mints a new key and secrets.enc becomes unreadable. Every
Python service that resolves vigil_path() must see the same volume, and the
API must see the daemon's workdirs. CI never runs compose, so this is the
check that keeps the mounts from regressing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"

# Mount targets are the image's pre-created, uid-1000 dirs (Dockerfile.backend,
# Dockerfile.daemon); a named volume mounted anywhere else comes up root-owned.
STATE_DIR = "/home/vigil/.vigil"
INVESTIGATIONS_DIR = "/app/data/investigations"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8")) or {}


def _named_volume_at(compose: dict, service: str, target: str) -> str | None:
    for entry in compose["services"][service].get("volumes") or []:
        if isinstance(entry, str):
            source, _, rest = entry.partition(":")
            if rest.split(":")[0] == target:
                return source if source in (compose.get("volumes") or {}) else None
        elif entry.get("target") == target and entry.get("type", "volume") == "volume":
            return entry.get("source")
    return None


@pytest.mark.parametrize(
    ("target", "services"),
    [
        (STATE_DIR, ("backend", "soc-daemon", "llm-worker")),
        (INVESTIGATIONS_DIR, ("backend", "soc-daemon")),
    ],
)
def test_services_share_one_named_volume(target: str, services: tuple) -> None:
    compose = _compose()
    sources = {s: _named_volume_at(compose, s, target) for s in services}
    assert all(sources.values()), f"no named volume at {target}: {sources}"
    assert len(set(sources.values())) == 1, f"{target} is not shared: {sources}"
