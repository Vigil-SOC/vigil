"""Read the last successful snapshot time from the state directory.

``core.backup.schedule`` writes one ``backup_status.json`` object. This
module only reads ``last_success_at``. A missing file, bad JSON, or an
unparseable timestamp is no timestamp.
"""

import json
from datetime import datetime

from core.config import vigil_path

STATUS_FILENAME = "backup_status.json"


def read_last_success_at() -> datetime | None:
    """Aware ``last_success_at``, or None when the status file cannot say."""
    path = vigil_path(STATUS_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("last_success_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # The writer uses datetime.now(timezone.utc).isoformat(), which is aware.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
