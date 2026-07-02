"""Frontend logging endpoint."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Create dedicated logger for frontend logs
frontend_logger = logging.getLogger('frontend')
frontend_logger.setLevel(logging.DEBUG)

# Resolve the frontend log file path.
log_file = Path("logs") / "frontend-app.log"

# Configure a handler if not already configured. This runs at import time,
# so it must never raise: a non-writable working directory (e.g. a
# read-only or non-root container filesystem) previously crashed the whole
# backend on startup with PermissionError (issue #376). Fall back to a
# console handler if the log file cannot be created.
if not frontend_logger.handlers:
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_file)
    except OSError as exc:
        # Directory not writable (or similar) — degrade gracefully to
        # the console instead of taking down the process on import.
        logging.getLogger(__name__).warning(
            "Frontend file logging disabled, falling back to console: %s",
            exc,
        )
        handler = logging.StreamHandler()

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    frontend_logger.addHandler(handler)

    # Don't propagate to root logger (avoid duplicate logs)
    frontend_logger.propagate = False


class FrontendLogEntry(BaseModel):
    """Frontend log entry model."""
    level: str
    message: str
    component: str
    timestamp: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


@router.post("/frontend")
async def log_frontend(entry: FrontendLogEntry):
    """
    Receive logs from frontend and write to file.
    
    Args:
        entry: Log entry with level, message, component, and optional extra data
    
    Returns:
        Status confirmation
    """
    try:
        # Build log message
        log_message = f"[{entry.component}] {entry.message}"
        
        # Append extra data if present
        if entry.extra:
            # Format extra data nicely
            extra_str = ", ".join([f"{k}={v}" for k, v in entry.extra.items()])
            log_message += f" ({extra_str})"
        
        # Log at appropriate level
        level = entry.level.upper()
        if level == 'DEBUG':
            frontend_logger.debug(log_message)
        elif level == 'INFO':
            frontend_logger.info(log_message)
        elif level == 'WARN' or level == 'WARNING':
            frontend_logger.warning(log_message)
        elif level == 'ERROR':
            frontend_logger.error(log_message)
        else:
            frontend_logger.info(log_message)
        
        return {"status": "ok"}
    
    except Exception as e:
        # Don't fail the request if logging fails
        logging.error(f"Error processing frontend log: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/frontend/status")
async def get_frontend_log_status():
    """Check if frontend logging is working."""
    return {
        "enabled": True,
        "log_file": str(log_file),
        "exists": log_file.exists(),
        "size": log_file.stat().st_size if log_file.exists() else 0
    }

