"""Tell the daemon that the API process just stored findings.

The daemon triages what it stores itself; findings the API stores (uploads,
sample alerts, API-hosted webhooks) reach it only through this Redis message,
with its timer-driven backfill sweep as the fallback. Call it from the API
call site, never from the storage layer: the daemon stores through the same
layer and would re-triage its own findings.
"""

import json
import logging
from typing import Iterable

from core.redis_client import open_sync_redis, redact_logged_text

logger = logging.getLogger(__name__)

CHANNEL = "vigil:daemon:triage"
# Ids carried per message; a larger import's tail is left to the backfill sweep.
MAX_IDS = 500


def signal_new_findings(finding_ids: Iterable[str]) -> None:
    """Publish the stored ids. Never raises: the sweep covers a missed signal."""
    ids = list(dict.fromkeys(i for i in finding_ids if i))[:MAX_IDS]
    if not ids:
        return
    try:
        with open_sync_redis(socket_connect_timeout=1, socket_timeout=1) as client:
            client.publish(CHANNEL, json.dumps(ids))
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "Triage signal not sent (%s); the backfill sweep will pick up",
            redact_logged_text(str(e)),
        )
