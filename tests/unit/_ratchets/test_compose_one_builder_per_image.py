"""No two compose services build the same image tag (#2455).

Compose bakes every `build:` in parallel. Two services that build one `image:`
both export it, and on a fresh `docker compose build` the second fails with
'image ... already exists'; a rebuild passes only because the tag now exists.
So one service builds each tag, and every other service that runs it reaches
that builder through depends_on, so `compose up <service>` still builds it.
CI never runs compose, so this is the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILES = sorted(
    p
    for pattern in (
        "docker-compose*.yml",
        "docker-compose*.yaml",
        "compose*.yml",
        "compose*.yaml",
    )
    for p in REPO.glob(f"**/{pattern}")
    if "node_modules" not in p.parts
)


def _services(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "services"
    ) or {}


def _depends_on(spec: dict) -> list[str]:
    deps = spec.get("depends_on") or []
    return list(deps) if isinstance(deps, (list, dict)) else []


def _reaches(services: dict, start: str, target: str) -> bool:
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name == target:
            return True
        if name in seen or name not in services:
            continue
        seen.add(name)
        stack.extend(_depends_on(services[name]))
    return False


def _builders(services: dict) -> dict[str, list[str]]:
    by_image: dict[str, list[str]] = {}
    for name, spec in services.items():
        if spec.get("build") is not None and spec.get("image"):
            by_image.setdefault(str(spec["image"]), []).append(name)
    return by_image


def test_compose_files_found() -> None:
    assert REPO / "infra" / "docker" / "docker-compose.yml" in COMPOSE_FILES


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_one_service_builds_each_image(path: Path) -> None:
    shared = {
        image: names
        for image, names in _builders(_services(path)).items()
        if len(names) > 1
    }
    assert (
        not shared
    ), f"{path.relative_to(REPO)}: more than one service builds {shared}"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_image_users_wait_on_its_builder(path: Path) -> None:
    services = _services(path)
    builder = {image: names[0] for image, names in _builders(services).items()}
    orphans = [
        f"{name} ({spec['image']}, built by {builder[str(spec['image'])]})"
        for name, spec in services.items()
        if spec.get("build") is None
        and str(spec.get("image")) in builder
        and not _reaches(services, name, builder[str(spec["image"])])
    ]
    assert not orphans, (
        f"{path.relative_to(REPO)}: these run a locally built image without "
        f"depending on the service that builds it: {orphans}"
    )
