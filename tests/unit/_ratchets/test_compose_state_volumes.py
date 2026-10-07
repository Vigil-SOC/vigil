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


def _compose_default(source: str) -> str:
    # ${VIGIL_BACKUP_STATE_DIR:-vigil_home} is the named volume unless overridden.
    if source.startswith("${") and source.endswith("}") and ":-" in source:
        return source.split(":-", 1)[1][:-1]
    return source


def _split_volume(entry: str) -> tuple[str, str] | None:
    # ${VAR:-name}:/container/path. The default may contain a colon, so split
    # the container path from the right.
    body = entry
    if body.endswith(":ro") or body.endswith(":rw"):
        body = body.rsplit(":", 1)[0]
    if ":" not in body:
        return None
    source, target = body.rsplit(":", 1)
    if not target.startswith("/"):
        return None
    return source, target


def _named_volume_at(compose: dict, service: str, target: str) -> str | None:
    volumes = compose.get("volumes") or {}
    for entry in compose["services"][service].get("volumes") or []:
        if isinstance(entry, str):
            split = _split_volume(entry)
            if split is None or split[1] != target:
                continue
            source = _compose_default(split[0])
            return source if source in volumes else None
        elif entry.get("target") == target and entry.get("type", "volume") == "volume":
            source = _compose_default(str(entry.get("source") or ""))
            return source if source in volumes else None
    return None


@pytest.mark.parametrize(
    ("target", "services"),
    [
        (STATE_DIR, ("backend", "soc-daemon", "llm-worker", "backup")),
        (INVESTIGATIONS_DIR, ("backend", "soc-daemon", "backup")),
    ],
)
def test_services_share_one_named_volume(target: str, services: tuple) -> None:
    compose = _compose()
    sources = {s: _named_volume_at(compose, s, target) for s in services}
    assert all(sources.values()), f"no named volume at {target}: {sources}"
    assert len(set(sources.values())) == 1, f"{target} is not shared: {sources}"
