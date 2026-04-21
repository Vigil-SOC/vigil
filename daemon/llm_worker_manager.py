"""LLM Worker Manager — dynamically starts/stops the ARQ worker subprocess.

The ARQ worker (``services.run_llm_worker``) processes queued Claude API
calls.  Because ARQ's ``run_worker()`` blocks, it must live in a separate
process.  This manager runs as an async task inside the daemon and polls
the ``orchestrator.settings`` SystemConfig key every few seconds, reading
the ``enabled`` field.  When the orchestrator is enabled the worker
subprocess is started; when disabled it is stopped.  If the worker
crashes while enabled it is automatically restarted on the next poll
cycle.
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = str(Path(__file__).parent.parent)

# How often (seconds) we check the DB flag and worker health.
_POLL_INTERVAL = 5


class LLMWorkerManager:
    """Manage the LLM worker as a child subprocess of the daemon."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Public API (called by SOCDaemon)
    # ------------------------------------------------------------------

    async def run(self, shutdown_event: asyncio.Event):
        """Main loop — poll DB, start/stop worker subprocess."""
        logger.info("LLM Worker Manager started")

        while not shutdown_event.is_set():
            self._sync_enabled_from_db()

            if self._enabled and not self._is_running():
                self._start_worker()
            elif not self._enabled and self._is_running():
                self._stop_worker()

            # Sleep but wake up immediately on shutdown.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=_POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

        # Daemon is shutting down — always stop the worker.
        self._stop_worker()
        logger.info("LLM Worker Manager shutdown complete")

    # ------------------------------------------------------------------
    # DB sync (same pattern as daemon/orchestrator.py)
    # ------------------------------------------------------------------

    def _sync_enabled_from_db(self):
        """Read the orchestrator enabled state from the single
        ``orchestrator.settings`` SystemConfig row."""
        try:
            from database.connection import get_db_manager
            from database.models import SystemConfig

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

    # ------------------------------------------------------------------
    # Subprocess lifecycle
    # ------------------------------------------------------------------

    def _start_worker(self):
        """Spawn the ARQ worker as a child process."""
        env = {**os.environ, "PYTHONPATH": PROJECT_ROOT}
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "services.run_llm_worker"],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("LLM Worker started (PID: %d)", self._process.pid)
        except Exception as exc:
            logger.error("Failed to start LLM Worker: %s", exc)
            self._process = None

    def _stop_worker(self):
        """Terminate the worker subprocess gracefully."""
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
        """Check whether the worker subprocess is alive."""
        return self._process is not None and self._process.poll() is None
