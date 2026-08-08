"""Host-native Ollama supervisor.

Ollama runs as a host process, not a container: Docker on macOS has no Metal
passthrough, so a containerized Ollama would be CPU-only.

Three constraints shape every function here:

- **Liveness is always an HTTP probe, never a held handle.** uvicorn ``--reload``
  respawns the server process on any edit under ``core/`` or ``services/``,
  destroying in-memory state. A ``Popen`` handle as source of
  truth would report Ollama down seconds after starting it, because someone
  saved a file. Probing also makes spawn idempotent across reloads.
- **The running Ollama is often not ours** — ``brew services`` (launchd) and
  Ollama.app both bind 11434. So Vigil never stops it, and never pattern-kills:
  that would take down a user's own instance and lose a race with launchd.
- **The spawned process must outlive its parent's process group.**
  ``start_new_session=True`` keeps Ctrl+C on ``./start.sh`` from killing it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from core.config import get_settings
from core.platform.service_contract import ActionResult, ServiceSpec, ServiceStatus

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PIDFILE = REPO_ROOT / "logs" / "ollama.pid"
LOGFILE = REPO_ROOT / "logs" / "ollama.log"

_FALLBACK_PATHS = ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama")
_SPAWN_LOCK = threading.Lock()


def base_url() -> str:
    """Host-side Ollama URL — what the backend/worker/daemon should call.

    ``OLLAMA_URL`` holds the host-side value (``.env`` ships localhost:11434).
    See :func:`container_base_url` for the container-side form.
    """
    return get_settings().ollama_url.strip().rstrip("/")


def container_base_url() -> str:
    """``base_url`` as a *container* must address it.

    Ollama runs on the host, so a container reaching it needs
    ``host.docker.internal``. Compose already defaults to that — but
    ``scripts/lib.sh::load_env`` exports the root ``.env`` OLLAMA_URL into the
    shell, and shell env beats a compose default, so containers would otherwise
    inherit a ``localhost`` that resolves to themselves (Bifrost then can't
    reach Ollama at all).

    Applied at the two places that shell out to compose — the ``_compose`` env
    in ``core/platform/service_manager.py`` and ``dc()`` in ``scripts/lib.sh`` — so
    there is one variable and one rewrite rule, not two configs to keep in sync.
    """
    url = base_url()
    for host in ("localhost", "127.0.0.1", "0.0.0.0"):
        url = url.replace(f"//{host}:", "//host.docker.internal:")
    return url


def ollama_ping(base_url: Optional[str] = None, timeout: float = 2.0) -> bool:
    """Cheap liveness probe: is an Ollama serving ``/api/tags`` at ``base_url``?

    Deliberately sync and uncached — it is polled every ~250ms while waiting
    for a spawned ``ollama serve`` to come up, which rules out
    :func:`fetch_ollama_models` (async, plus an ``/api/show`` per model).
    """
    base = (
        (base_url or get_settings().ollama_url)
        .strip()
        .rstrip("/")
    )
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            return client.get(f"{base}/api/tags").status_code == 200
    except Exception:  # noqa: BLE001 — any failure means "not serving"
        return False


def _no_post_start_sync() -> dict:
    """Default :func:`start` hook: do nothing.

    Starting Ollama alone accomplishes nothing user-visible — LLM traffic is
    dispatched through Bifrost, so the model catalog has to be refreshed
    before anything is selectable. Platform must not import a capability
    domain to say that, so a composition root that may depend on both tiers
    passes the refresh in: ``services/api/routers/local_services.py`` for the
    API path, ``scripts/ollama_supervise.py`` for the CLI one.
    """
    return {}


def binary_path() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    return next((p for p in _FALLBACK_PATHS if os.path.exists(p)), None)


def _read_pid() -> Optional[int]:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_is_ollama(pid: int) -> bool:
    """Guard against a stale pidfile pointing at a recycled PID."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and "ollama" in r.stdout.strip().lower()


def _managed_by_vigil() -> bool:
    pid = _read_pid()
    return pid is not None and _pid_is_ollama(pid)


def status(spec: ServiceSpec) -> ServiceStatus:
    installed = binary_path() is not None
    running = ollama_ping(base_url())
    if running:
        state = (
            "running (started by Vigil)"
            if _managed_by_vigil()
            else "running (external)"
        )
    elif installed:
        state = "stopped"
    else:
        state = "not installed"
    return ServiceStatus(
        name=spec.name,
        kind="host",
        running=running,
        ready=running,
        status=state,
        installed=installed,
        managed_by_vigil=running and _managed_by_vigil(),
        startable=spec.startable,
        stoppable=spec.stoppable,
        description=spec.description,
        detail=None if installed else "Install with: brew install ollama",
    )


def start(
    spec: ServiceSpec,
    *,
    timeout: int = 30,
    post_start_sync: Callable[[], dict] = _no_post_start_sync,
) -> ActionResult:
    url = base_url()
    if ollama_ping(url):
        return ActionResult(
            True,
            "Ollama already running",
            already_running=True,
            detail=post_start_sync(),
        )

    exe = binary_path()
    if exe is None:
        return ActionResult(
            False,
            "Ollama binary not found. Install it with `brew install ollama` — "
            "Vigil runs Ollama natively (a container would be CPU-only on macOS) "
            "and will not fall back to Docker.",
            code="not_installed",
        )

    with _SPAWN_LOCK:
        if ollama_ping(url):  # won the race while waiting on the lock
            return ActionResult(
                True,
                "Ollama already running",
                already_running=True,
                detail=post_start_sync(),
            )
        try:
            LOGFILE.parent.mkdir(parents=True, exist_ok=True)
            # A file handle, never PIPE: nothing drains a pipe here, so ollama
            # would block once the ~64KB buffer filled.
            log_fh = open(LOGFILE, "ab")
            proc = subprocess.Popen(
                [exe, "serve"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(REPO_ROOT),
            )
        except OSError as e:
            return ActionResult(
                False, f"Failed to spawn ollama: {e}", code="spawn_error"
            )
        try:
            PIDFILE.write_text(str(proc.pid))
        except OSError as e:
            logger.warning("Could not write %s: %s", PIDFILE, e)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ollama_ping(url):
            return ActionResult(True, "Ollama started", detail=post_start_sync())
        if proc.poll() is not None:
            return ActionResult(
                False,
                f"ollama serve exited with code {proc.returncode}. " f"See {LOGFILE}.",
                code="exited",
            )
        time.sleep(0.25)
    return ActionResult(
        False,
        f"Ollama did not become ready within {timeout}s. See {LOGFILE}.",
        code="timeout",
    )
