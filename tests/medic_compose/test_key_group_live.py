"""S6-3 / V2-7, live: the API key file `root:10010 0640` and who can read it.

On Docker Desktop Compose's bind-mounted secret hides ownership (fakeowner), so
the full-stack live test can't show the Linux case. This one puts the file on a
Docker volume (the VM's Linux filesystem, real permission checks), owned and
moded exactly as `enable-compose.sh` leaves it on Linux, and opens it from each
real image as the uid and groups Compose gives that service.

Opt-in: VIGIL_MEDIC_COMPOSE_LIVE=1. Images: vigil-medic, vigil-medic-gateway and
vigil-backend at VIGIL_MEDIC_IMAGE_TAG (default `local`, what Compose builds).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest
import yaml

from tests.medic_compose.compose import BASE, OVERLAY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("VIGIL_MEDIC_COMPOSE_LIVE") != "1",
        reason="live Docker test: set VIGIL_MEDIC_COMPOSE_LIVE=1",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker"),
]

TAG = os.environ.get("VIGIL_MEDIC_IMAGE_TAG", "local")
KEY = "k" * 43
READ = "import sys; print(open(sys.argv[1]).read().strip() == sys.argv[2])"


def _service(path, name):
    return yaml.safe_load(path.read_text())["services"][name]


def _identity(name: str) -> tuple[str, list[str]]:
    """(user, extra groups) exactly as the Compose files give them."""
    base = _service(BASE, name)
    overlay = (yaml.safe_load(OVERLAY.read_text())["services"] or {}).get(name, {})
    user = base.get("user", "1000:1000")  # the backend image runs as 1000:1000
    groups = [str(g) for g in base.get("group_add", []) + overlay.get("group_add", [])]
    return user, groups


@pytest.fixture(scope="module")
def volume():
    name = f"medic-s7b-keygroup-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "volume", "create", name], check=True, capture_output=True)
    try:
        subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none", "--user", "0:0",
                "-v", f"{name}:/s", "--entrypoint", "sh", f"vigil-medic-gateway:{TAG}",
                "-c", f"printf %s {KEY} > /s/api_key && chown 0:10010 /s/api_key "
                "&& chmod 0640 /s/api_key && stat -c '%u:%g %a' /s/api_key",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        yield name
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True)


def _reads(volume: str, image: str, user: str, groups: list[str]) -> str:
    args = ["docker", "run", "--rm", "--network", "none", "--user", user]
    for g in groups:
        args += ["--group-add", g]
    args += ["-v", f"{volume}:/run/secrets:ro", "--entrypoint", "python", image]
    done = subprocess.run(
        [*args, "-c", READ, "/run/secrets/api_key", KEY],
        capture_output=True,
        text=True,
        check=False,
    )
    if "PermissionError" in done.stderr:
        return "denied"
    assert done.returncode == 0, done.stderr
    return "read" if done.stdout.strip() == "True" else "wrong"


@pytest.mark.parametrize(
    "service, image",
    [("medic", "vigil-medic"), ("backend", "vigil-backend")],
)
def test_the_two_key_readers_read_it(volume, service, image) -> None:
    user, groups = _identity(service)
    assert groups == ["10010"]
    assert _reads(volume, f"{image}:{TAG}", user, groups) == "read"


@pytest.mark.parametrize(
    "service, image",
    [("medic", "vigil-medic"), ("backend", "vigil-backend")],
)
def test_without_the_group_they_could_not(volume, service, image) -> None:
    user, _ = _identity(service)
    assert _reads(volume, f"{image}:{TAG}", user, []) == "denied"


def test_the_gateway_cannot_read_it(volume) -> None:
    user, groups = _identity("medic-gateway")
    assert groups == []
    assert _reads(volume, f"vigil-medic-gateway:{TAG}", user, groups) == "denied"


def test_gid_1000_alone_no_longer_reads_it(volume) -> None:
    # What V2-7 built (10001:1000): a host user in group 1000 could read it.
    assert _reads(volume, f"vigil-medic:{TAG}", "1234:1000", []) == "denied"
