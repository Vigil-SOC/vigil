"""Session lifecycle management for multi-turn chat conversations.

L1: in-memory dict (fast, per-process).
L2: MemPalace-backed JSON files (durable across restarts).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionManager:
    """Owns in-memory session storage with MemPalace write-through."""

    def __init__(self) -> None:
        self.sessions: Dict[str, List[dict]] = {}
        self.summaries: Dict[str, str] = {}
        self._mempalace = None  # lazy-init: Path | False

    # ------------------------------------------------------------------
    # MemPalace path resolution
    # ------------------------------------------------------------------

    def _get_sessions_dir(self) -> Optional[Path]:
        if self._mempalace is None:
            try:
                from services.mempalace_paths import get_palace_path

                sessions_dir = get_palace_path() / "sessions"
                sessions_dir.mkdir(parents=True, exist_ok=True)
                self._mempalace = sessions_dir
            except Exception as exc:
                logger.debug("MemPalace sessions dir init failed: %s", exc)
                self._mempalace = False
        return self._mempalace if self._mempalace else None

    # ------------------------------------------------------------------
    # Persistence (fire-and-forget)
    # ------------------------------------------------------------------

    def persist_async(self, session_id: str) -> None:
        """Write session to disk in a daemon thread; never blocks the caller."""
        messages = list(self.sessions.get(session_id, []))
        summary = self.summaries.get(session_id, "")

        def _write() -> None:
            sessions_dir = self._get_sessions_dir()
            if not sessions_dir:
                return
            try:
                import os

                safe_id = session_id.replace("/", "_").replace("\\", "_")
                tmp = sessions_dir / f"{safe_id}.json.tmp"
                dest = sessions_dir / f"{safe_id}.json"
                tmp.write_text(
                    json.dumps(
                        {
                            "messages": messages,
                            "message_count": len(messages),
                            "summary": summary,
                        }
                    )
                )
                os.replace(tmp, dest)
            except Exception as exc:
                logger.debug("MemPalace session persist failed: %s", exc)

        threading.Thread(target=_write, daemon=True).start()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self, session_id: str, initial_context: Optional[List[dict]] = None
    ) -> str:
        self.sessions[session_id] = list(initial_context or [])
        self.persist_async(session_id)
        return session_id

    def get(self, session_id: str) -> Optional[List[dict]]:
        if session_id in self.sessions:
            return self.sessions[session_id]

        # L2 restore
        sessions_dir = self._get_sessions_dir()
        if sessions_dir:
            try:
                safe_id = session_id.replace("/", "_").replace("\\", "_")
                path = sessions_dir / f"{safe_id}.json"
                if path.exists():
                    data = json.loads(path.read_text())
                    messages = data.get("messages", [])
                    if messages:
                        self.sessions[session_id] = messages
                        if data.get("summary"):
                            self.summaries[session_id] = data["summary"]
                        logger.info(
                            "Restored session %s from MemPalace (%d messages)",
                            session_id,
                            len(messages),
                        )
                        return self.sessions[session_id]
            except Exception as exc:
                logger.debug("MemPalace session restore failed: %s", exc)

        return None

    def clear(self, session_id: str) -> bool:
        if session_id in self.sessions:
            del self.sessions[session_id]
            self.summaries.pop(session_id, None)
            return True
        return False

    # ------------------------------------------------------------------
    # Rolling summary
    # ------------------------------------------------------------------

    def get_summary(self, session_id: str) -> str:
        return self.summaries.get(session_id, "")

    def update_summary(self, session_id: str, text: str) -> None:
        self.summaries[session_id] = text
        self.persist_async(session_id)
