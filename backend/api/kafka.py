"""Kafka ingestion API endpoints.

Provides configuration + live status for the Kafka consumer that runs
inside the daemon. Config is persisted in the ``kafka.settings``
SystemConfig row; secrets (SASL password, SSL cert path) are
intentionally NOT accepted here — they must be set via env vars.

Live stats (messages consumed, last message time, error counters) are
fetched from the daemon's health server on ``DAEMON_HEALTH_PORT``.
If the daemon isn't reachable, the endpoint returns the persisted
config with ``connected=false`` so the UI still renders.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kafka", tags=["kafka"])
logger = logging.getLogger(__name__)

SYSTEMCONFIG_KEY = "kafka.settings"
DAEMON_STATUS_TIMEOUT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class KafkaConfigBody(BaseModel):
    """Non-secret Kafka settings persisted in SystemConfig."""

    enabled: bool = False
    bootstrap_servers: str = "localhost:9092"
    consumer_group: str = "vigil-soc"
    topics: List[str] = Field(default_factory=list)
    auto_offset_reset: str = "latest"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    max_poll_records: int = 500
    session_timeout_ms: int = 30_000


def _default_config() -> Dict[str, Any]:
    return KafkaConfigBody().model_dump()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _daemon_status_url() -> str:
    host = os.getenv("DAEMON_HEALTH_HOST", "localhost")
    port = os.getenv("DAEMON_HEALTH_PORT", "9091")
    return f"http://{host}:{port}/status"


async def _fetch_daemon_kafka_stats() -> Optional[Dict[str, Any]]:
    """Pull the kafka sub-object from the daemon's /status endpoint."""
    url = _daemon_status_url()
    try:
        timeout = aiohttp.ClientTimeout(total=DAEMON_STATUS_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
                return body.get("kafka") or None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug("Daemon status unreachable at %s: %s", url, e)
        return None
    except Exception as e:
        logger.debug("Daemon status fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_kafka_config() -> Dict[str, Any]:
    """Return the persisted Kafka config, falling back to defaults."""
    from database.config_service import get_config_service

    try:
        stored = get_config_service().get_system_config(SYSTEMCONFIG_KEY) or {}
    except Exception as e:
        logger.error("Failed to read kafka config: %s", e)
        raise HTTPException(500, f"Failed to read kafka config: {e}")

    merged = {**_default_config(), **(stored if isinstance(stored, dict) else {})}
    # SASL password is env-only; never return it from the API
    merged.pop("sasl_password", None)
    return merged


@router.put("/config")
async def put_kafka_config(body: KafkaConfigBody) -> Dict[str, Any]:
    """Upsert the persisted Kafka config. Secrets are not accepted here."""
    from database.config_service import get_config_service

    payload = body.model_dump()
    try:
        ok = get_config_service().set_system_config(
            key=SYSTEMCONFIG_KEY,
            value=payload,
            description="Kafka ingestion settings",
            config_type="ingestion",
            change_reason="Updated via /api/kafka/config",
        )
        if not ok:
            raise HTTPException(500, "Failed to save kafka config")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to write kafka config: %s", e)
        raise HTTPException(500, f"Failed to save kafka config: {e}")

    return {"status": "ok", "config": payload}


@router.get("/status")
async def get_kafka_status() -> Dict[str, Any]:
    """Return live consumer stats plus the persisted enabled/config state."""
    from database.config_service import get_config_service

    try:
        stored = get_config_service().get_system_config(SYSTEMCONFIG_KEY) or {}
    except Exception:
        stored = {}
    merged = {**_default_config(), **(stored if isinstance(stored, dict) else {})}
    merged.pop("sasl_password", None)

    stats = await _fetch_daemon_kafka_stats()
    daemon_reachable = stats is not None
    if stats is None:
        stats = {
            "connected": False,
            "messages_consumed": 0,
            "messages_enqueued": 0,
            "duplicates_skipped": 0,
            "decode_errors": 0,
            "missing_id_errors": 0,
            "last_message_at": None,
            "last_error": None,
            "last_error_at": None,
            "topics": merged.get("topics", []),
            "consumer_group": merged.get("consumer_group", ""),
        }

    return {
        "enabled": bool(merged.get("enabled", False)),
        "daemon_reachable": daemon_reachable,
        "config": merged,
        "stats": stats,
    }
