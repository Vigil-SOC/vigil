"""Local service orchestration — the sanctioned channel for start/stop/status.

Covers docker-compose services and the host-native Ollama process behind one
API. The ``SERVICES`` registry is the allowlist: a name that isn't a key here
can never reach a subprocess, which is what keeps the ``{name}`` path param on
``backend/api/local_services.py`` safe.

Compose invariants worth preserving:

- Profiles are selected with ``COMPOSE_PROFILES``, not ``--profile``. It works
  on both compose v1 (>=1.28) and v2 and needs no global-flag ordering, so
  ``scripts/lib.sh`` can use the identical mechanism.
- No ``-p``/``--project-directory``. Compose derives the project name from the
  compose file's parent directory; passing one would orphan every container
  ``start.sh`` created via ``scripts/lib.sh::dc``.
- ``cwd`` is the repo root and the compose file is passed by absolute path, so
  behaviour doesn't depend on where the backend was launched from.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker" / "docker-compose.yml"


@dataclass(frozen=True)
class ServiceSpec:
    """A managed service. ``container is None`` means host-native."""

    name: str
    container: Optional[str]
    compose_service: Optional[str]
    profile: Optional[str] = None
    startable: bool = True
    stoppable: bool = True
    description: str = ""


# postgres/redis/bifrost are stoppable=False on purpose: a UI stop button on
# them takes the running application down with it.
SERVICES: Dict[str, ServiceSpec] = {
    "postgres": ServiceSpec(
        "postgres",
        "deeptempo-postgres",
        "postgres",
        stoppable=False,
        description="Local PostgreSQL container",
    ),
    "redis": ServiceSpec(
        "redis",
        "deeptempo-redis",
        "redis",
        stoppable=False,
        description="Redis (ARQ job queue)",
    ),
    "bifrost": ServiceSpec(
        "bifrost",
        "deeptempo-bifrost",
        "bifrost",
        stoppable=False,
        description="LLM gateway",
    ),
    "pgadmin": ServiceSpec(
        "pgadmin", "deeptempo-pgadmin", "pgadmin", "dev", description="pgAdmin"
    ),
    "splunk": ServiceSpec(
        "splunk", "deeptempo-splunk", "splunk", "splunk", description="Splunk"
    ),
    "kafka": ServiceSpec(
        "kafka", "deeptempo-kafka", "kafka", "kafka", description="Kafka ingestion"
    ),
    "jaeger": ServiceSpec(
        "jaeger",
        "deeptempo-jaeger",
        "jaeger",
        "observability",
        description="Jaeger tracing",
    ),
    "prometheus": ServiceSpec(
        "prometheus",
        "deeptempo-prometheus",
        "prometheus",
        "observability",
        description="Prometheus",
    ),
    "grafana": ServiceSpec(
        "grafana",
        "deeptempo-grafana",
        "grafana",
        "observability",
        description="Grafana",
    ),
    "otel-collector": ServiceSpec(
        "otel-collector",
        "deeptempo-otel-collector",
        "otel-collector",
        "observability",
        description="OpenTelemetry collector",
    ),
    # Host-native (Docker on macOS has no Metal passthrough, so a container
    # would be CPU-only). Never stopped: see services/ollama_process.py.
    "ollama": ServiceSpec(
        "ollama",
        None,
        None,
        stoppable=False,
        description="Ollama (host-native LLM runtime)",
    ),
}


@dataclass
class ServiceStatus:
    name: str
    kind: str
    running: bool
    status: str
    installed: bool = True
    ready: bool = False
    managed_by_vigil: bool = False
    startable: bool = True
    stoppable: bool = True
    description: str = ""
    detail: Optional[str] = None


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    already_running: bool = False
    code: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)


class UnknownServiceError(KeyError):
    """Raised when a name isn't in the SERVICES allowlist."""


def _spec(name: str) -> ServiceSpec:
    try:
        return SERVICES[name]
    except KeyError:
        raise UnknownServiceError(name) from None


def _dc_cmd() -> List[str]:
    return (
        ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]
    )


def docker_available() -> tuple[bool, str]:
    """Whether the Docker *daemon* is reachable — not merely installed."""
    if not shutil.which("docker"):
        return False, "docker CLI not found on PATH"
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "docker info timed out"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        return (
            False,
            (r.stderr.strip().splitlines() or ["docker daemon not reachable"])[0],
        )
    return True, r.stdout.strip()


def _compose(
    args: List[str], profile: Optional[str], timeout: int
) -> subprocess.CompletedProcess:
    from services.ollama_process import container_base_url

    env = os.environ.copy()
    if profile:
        env["COMPOSE_PROFILES"] = profile
    # Containers reach the host-native Ollama via host.docker.internal; the
    # ambient OLLAMA_URL is the host-side value. See ollama_process.
    env["OLLAMA_URL"] = container_base_url()
    return subprocess.run(
        [*_dc_cmd(), "-f", str(COMPOSE_FILE), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _container_state(container: str) -> tuple[bool, str]:
    """(running, status). Anchored name match so `-test` suffixes don't alias."""
    try:
        r = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name=^{container}$",
                "--format",
                "{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip()
        r = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^{container}$",
                "--format",
                "{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return False, r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"error: {e}"
    return False, "not found"


def status(name: str) -> ServiceStatus:
    spec = _spec(name)
    if spec.container is None:
        from services import ollama_process

        return ollama_process.status(spec)

    ok, detail = docker_available()
    if not ok:
        return ServiceStatus(
            name=spec.name,
            kind="docker",
            running=False,
            status="docker unavailable",
            startable=spec.startable,
            stoppable=spec.stoppable,
            description=spec.description,
            detail=detail,
        )
    running, state = _container_state(spec.container)
    return ServiceStatus(
        name=spec.name,
        kind="docker",
        running=running,
        ready=running,
        status=state,
        managed_by_vigil=running,
        startable=spec.startable,
        stoppable=spec.stoppable,
        description=spec.description,
    )


def start(name: str, *, wait: bool = False, timeout: int = 120) -> ActionResult:
    spec = _spec(name)
    if not spec.startable:
        return ActionResult(
            False, f"{name} cannot be started by Vigil", code="not_startable"
        )
    if spec.container is None:
        from services import ollama_process

        return ollama_process.start(spec, timeout=min(timeout, 60))

    ok, detail = docker_available()
    if not ok:
        return ActionResult(
            False, f"Docker daemon not reachable: {detail}", code="docker_unavailable"
        )
    if _container_state(spec.container)[0]:
        return ActionResult(True, f"{name} already running", already_running=True)
    return _compose_action(spec, ["up", "-d", spec.compose_service], "start", timeout)


def stop(name: str) -> ActionResult:
    spec = _spec(name)
    if not spec.stoppable:
        return ActionResult(
            False, f"{name} cannot be stopped by Vigil", code="not_stoppable"
        )
    if spec.container is None:
        return ActionResult(
            False, f"{name} is not managed by Vigil", code="not_stoppable"
        )
    return _compose_action(spec, ["stop", spec.compose_service], "stop", 60)


def restart(name: str) -> ActionResult:
    spec = _spec(name)
    if not spec.stoppable:
        return ActionResult(
            False, f"{name} cannot be restarted by Vigil", code="not_stoppable"
        )
    if spec.container is None:
        return ActionResult(
            False, f"{name} is not managed by Vigil", code="not_stoppable"
        )
    return _compose_action(spec, ["restart", spec.compose_service], "restart", 120)


def _compose_action(
    spec: ServiceSpec, args: List[str], verb: str, timeout: int
) -> ActionResult:
    try:
        r = _compose(args, spec.profile, timeout)
    except subprocess.TimeoutExpired:
        return ActionResult(
            False, f"docker compose {verb} timed out after {timeout}s", code="timeout"
        )
    except OSError as e:
        return ActionResult(False, str(e), code="exec_error")
    if r.returncode != 0:
        logger.error("compose %s %s failed: %s", verb, spec.name, r.stderr.strip())
        return ActionResult(
            False,
            f"Failed to {verb} {spec.name}",
            code="compose_error",
            detail={"stderr": r.stderr.strip()},
        )
    return ActionResult(
        True, f"Successfully {verb}ed {spec.name}", detail={"output": r.stdout.strip()}
    )


def list_services() -> List[ServiceStatus]:
    return [status(name) for name in SERVICES]
