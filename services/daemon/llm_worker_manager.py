# Supervises the ARQ worker as a child process of the daemon: polls the
# orchestrator.settings enabled flag and starts, stops or restarts to match it.

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

# The -m entrypoint, shared with compose, the Helm Deployment and start.sh.
WORKER_MODULE = "services.worker"

_POLL_INTERVAL = 5


class LLMWorkerManager:
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._enabled = False

    async def run(self, shutdown_event: asyncio.Event):
        logger.info("LLM Worker Manager started")

        while not shutdown_event.is_set():
            self._sync_enabled_from_db()

            if self._enabled and not self._is_running():
                self._start_worker()
            elif not self._enabled and self._is_running():
                self._stop_worker()

            try:  # sleep, but wake immediately on shutdown
                await asyncio.wait_for(shutdown_event.wait(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

        self._stop_worker()
        logger.info("LLM Worker Manager shutdown complete")

    def _sync_enabled_from_db(self):
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import SystemConfig

            with get_db_manager().session_scope() as session:
                cfg = (
                    session.query(SystemConfig)
                    .filter_by(key="orchestrator.settings")
                    .first()
                )
                if cfg and isinstance(cfg.value, dict):
                    db_enabled = bool(cfg.value.get("enabled", False))
                    if db_enabled != self._enabled:
                        self._enabled = db_enabled
                        logger.info(
                            "LLM Worker %s (synced from DB)",
                            "ENABLED" if db_enabled else "DISABLED",
                        )
        except Exception:
            pass  # DB not ready yet — keep previous state

    def _start_worker(self):
        # Exports the parent env into a child process; not a config read.
        env = {**os.environ, "PYTHONPATH": PROJECT_ROOT}  # noqa: ENV001
        log_path = Path(PROJECT_ROOT) / "logs" / "llm_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_file = open(log_path, "a")
            self._process = subprocess.Popen(
                [sys.executable, "-m", WORKER_MODULE],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=log_file,
                stderr=log_file,
            )
            logger.info(
                "LLM Worker started (PID: %d) — logs: %s",
                self._process.pid,
                log_path,
            )
        except Exception as exc:
            logger.error("Failed to start LLM Worker: %s", exc)
            self._process = None

    def _stop_worker(self):
        if not self._is_running():
            self._process = None
            return

        pid = self._process.pid
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("LLM Worker (PID %d) did not exit, killing", pid)
            self._process.kill()
            self._process.wait(timeout=5)

        logger.info("LLM Worker stopped (PID: %d)", pid)
        self._process = None

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None
