"""Working directory manager for autonomous investigations."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.time import utcnow

logger = logging.getLogger(__name__)


class WorkdirManager:
    """Manages working directories for sub-agent investigations.

    Each investigation gets an isolated directory with structured files
    that the agent reads/writes during its execution loop.
    """

    REQUIRED_FILES = {
        "state.json": "{}",
        "context.md": "",
        "log.jsonl": "",
    }

    def __init__(self, base_dir: str = "data/investigations"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create(self, investigation_id: str) -> Path:
        workdir = self.base_dir / investigation_id
        workdir.mkdir(parents=True, exist_ok=True)

        for filename, default_content in self.REQUIRED_FILES.items():
            filepath = workdir / filename
            if not filepath.exists():
                filepath.write_text(default_content, encoding="utf-8")

        logger.info(f"Created working directory: {workdir}")
        return workdir

    def exists(self, investigation_id: str) -> bool:
        return (self.base_dir / investigation_id).is_dir()

    def read_file(self, investigation_id: str, filename: str) -> str:
        filepath = self._safe_path(investigation_id, filename)
        if filepath and filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def write_file(self, investigation_id: str, filename: str, content: str) -> bool:
        filepath = self._safe_path(investigation_id, filename)
        if not filepath:
            return False
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        return True

    def append_file(self, investigation_id: str, filename: str, content: str) -> bool:
        filepath = self._safe_path(investigation_id, filename)
        if not filepath:
            return False
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(content)
        return True

    def list_files(self, investigation_id: str) -> List[str]:
        workdir = self.base_dir / investigation_id
        if not workdir.is_dir():
            return []
        result = []
        for p in sorted(workdir.rglob("*")):
            if p.is_file():
                result.append(str(p.relative_to(workdir)))
        return result

    def read_state(self, investigation_id: str) -> Dict[str, Any]:
        raw = self.read_file(investigation_id, "state.json")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Corrupt state.json for {investigation_id}")
            return {}

    def write_state(self, investigation_id: str, state: Dict[str, Any]):
        self.write_file(
            investigation_id, "state.json", json.dumps(state, indent=2, default=str)
        )

    def append_log(self, investigation_id: str, event: Dict[str, Any]):
        event.setdefault("ts", utcnow().isoformat())
        event.setdefault("vigil.investigation.id", investigation_id)
        # Embed current OTEL trace context so log lines are correlatable in Jaeger/Grafana
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.is_valid:
                event["trace_id"] = format(ctx.trace_id, "032x")
                event["span_id"] = format(ctx.span_id, "016x")
        except Exception:
            pass
        line = json.dumps(event, default=str) + "\n"
        self.append_file(investigation_id, "log.jsonl", line)

    def get_log(self, investigation_id: str, tail: int = 50) -> List[Dict]:
        raw = self.read_file(investigation_id, "log.jsonl")
        if not raw.strip():
            return []
        lines = raw.strip().split("\n")
        entries = []
        for line in lines[-tail:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _safe_path(self, investigation_id: str, filename: str) -> Optional[Path]:
        base = self.base_dir.resolve()
        workdir = (base / investigation_id).resolve()
        filepath = (workdir / filename).resolve()
        if workdir.parent != base or not filepath.is_relative_to(workdir):
            logger.error(
                f"Path outside workdir blocked: {investigation_id!r}/{filename!r}"
            )
            return None
        return filepath
