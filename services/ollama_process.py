"""Host-native Ollama supervisor.

Ollama runs as a host process, not a container: Docker on macOS has no Metal
passthrough, so a containerized Ollama would be CPU-only.

Three constraints shape every function here:

- **Liveness is always an HTTP probe, never a held handle.** uvicorn ``--reload``
  respawns the server process on any edit under ``backend/``/``services/``/
  ``database/``, destroying in-memory state. A ``Popen`` handle as source of
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
from typing import TYPE_CHECKING, Optional

from services.provider_model_discovery import ollama_ping

if TYPE_CHECKING:
    from services.service_manager import ActionResult, ServiceSpec, ServiceStatus

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
PIDFILE = REPO_ROOT / "logs" / "ollama.pid"
LOGFILE = REPO_ROOT / "logs" / "ollama.log"

_FALLBACK_PATHS = ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama")
_SPAWN_LOCK = threading.Lock()


def base_url() -> str:
    """Host-side Ollama URL — what the backend/worker/daemon should call.

    ``OLLAMA_URL`` holds the host-side value (``.env`` ships localhost:11434).
    See :func:`container_base_url` for the container-side form.
    """
    return (os.getenv("OLLAMA_URL") or "http://localhost:11434").strip().rstrip("/")


def container_base_url() -> str:
    """``base_url`` as a *container* must address it.

    Ollama runs on the host, so a container reaching it needs
    ``host.docker.internal``. Compose already defaults to that — but
    ``scripts/lib.sh::load_env`` exports the root ``.env`` OLLAMA_URL into the
    shell, and shell env beats a compose default, so containers would otherwise
    inherit a ``localhost`` that resolves to themselves (Bifrost then can't
    reach Ollama at all).

    Applied at the two places that shell out to compose — the ``_compose`` env
    in ``services/service_manager.py`` and ``dc()`` in ``scripts/lib.sh`` — so
    there is one variable and one rewrite rule, not two configs to keep in sync.
    """
    url = base_url()
    for host in ("localhost", "127.0.0.1", "0.0.0.0"):
        url = url.replace(f"//{host}:", "//host.docker.internal:")
    return url


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


def status(spec: "ServiceSpec") -> "ServiceStatus":
    from services.service_manager import ServiceStatus

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


def start(spec: "ServiceSpec", *, timeout: int = 30) -> "ActionResult":
    from services.service_manager import ActionResult

    url = base_url()
    if ollama_ping(url):
        return ActionResult(
            True, "Ollama already running", already_running=True, detail=_sync_bifrost()
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
                detail=_sync_bifrost(),
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
            return ActionResult(True, "Ollama started", detail=_sync_bifrost())
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


def _sync_bifrost() -> dict:
    """Push the freshly-reachable Ollama catalog into Bifrost's live config.

    Starting Ollama alone accomplishes nothing user-visible: LLM traffic is
    dispatched through Bifrost, and ``docker/bifrost/config.json`` is only a
    first-boot seed (live config lives in Bifrost's SQLite). Without this the
    button "succeeds" and no Ollama model is selectable.

    Mirrors ``backend/api/llm_providers.py::_schedule_catalog_resync`` (cache
    invalidate + sync), but awaits the sync rather than firing it off, so the
    caller can report ``bifrost_synced`` truthfully. Callers run in a threadpool
    thread with no running loop; if a loop *is* running we fall back to
    scheduling, since ``asyncio.run`` would raise. Best-effort throughout — a
    Bifrost that is still booting must not fail the start call.
    """
    import asyncio

    try:
        from services.bifrost_admin import sync_all_provider_models
        from services.model_registry import invalidate_model_cache

        invalidate_model_cache()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(sync_all_provider_models())
            return {"bifrost_synced": True}
        loop.create_task(sync_all_provider_models())
        return {"bifrost_synced": False, "bifrost_sync_scheduled": True}
    except Exception as e:  # noqa: BLE001
        logger.info("Bifrost model sync after Ollama start did not complete: %s", e)
        return {"bifrost_synced": False, "bifrost_sync_error": str(e)}


def main() -> int:
    """CLI entry so ``scripts/lib.sh`` reuses this spawn rather than copying it.

    macOS ships no ``setsid``, and a bare ``nohup ... &`` would leave Ollama in
    start.sh's process group where Ctrl+C kills it. Shelling back into this
    module keeps one implementation of the probe/pidfile/session semantics.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from services.service_manager import SERVICES

    result = start(SERVICES["ollama"])
    print(result.message)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
