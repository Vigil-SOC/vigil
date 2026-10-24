"""
Darktrace inbound webhook receiver.

Accepts pushes from Darktrace (SaaS tenants and on-prem master appliances)
for three alert streams — Model Breach, AI Analyst, and System Status —
verifies an HMAC-SHA256 signature against a shared secret, transforms each
payload into a Vigil finding via ``DarktraceIngestionService``, and ingests
it through the shared ``IngestionService``.

Endpoints:
    POST /api/webhooks/darktrace/model-breach
    POST /api/webhooks/darktrace/ai-analyst
    POST /api/webhooks/darktrace/system-status
    GET  /api/webhooks/darktrace/health

Signature header: ``X-Darktrace-Signature`` (hex HMAC-SHA256 of raw body).
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, Request, status

from core.config import get_settings
from core.integrations.darktrace.ingestion import DarktraceIngestionService
from core.routing import Auth, RouterMeta
from core.secrets import get_secret
from core.webhook_receiver import ingest, read_signed_json
from core.webhook_rejections import rejection_counts

logger = logging.getLogger(__name__)

router = APIRouter()


def darktrace_enabled() -> bool:
    """Master flag for the Darktrace webhook receiver.

    Off unless explicitly enabled. Lives here rather than inline in
    ``services/api/main.py`` so the gate travels with the receiver it guards and
    can be unit-tested directly (issue #478). Reads the typed ``Settings``
    field rather than the raw env var so there is a single source of truth
    for the flag, matching ``cloudflare_webhooks.cloudy_ingestion_enabled``.
    """
    return get_settings().darktrace_enabled


ROUTER_META = RouterMeta(
    prefix="/api/webhooks/darktrace",
    tags=["darktrace"],
    auth=Auth.PUBLIC_WEBHOOK,
    reason=(
        "Inbound receiver for Darktrace pushes — the caller is a machine, so "
        "there is no session to authenticate. Each endpoint verifies an "
        "HMAC-SHA256 X-Darktrace-Signature and fails closed when no shared "
        "secret is configured."
    ),
    # env.example and https://vigilsoc.org/docs/integrations/darktrace/
    # document DARKTRACE_ENABLED as the on/off toggle; leaving it unset
    # must leave the receiver off.
    enabled=darktrace_enabled,
)


def _get_settings() -> Dict[str, Any]:
    """Read darktrace.settings from system_config (DB). Falls back to env vars."""
    try:
        from core.storage.config_service import get_config_service

        value = get_config_service().get_system_config("darktrace.settings") or {}
        if value:
            return value
    except Exception as exc:  # noqa: BLE001
        logger.debug("darktrace.settings read failed, using env: %s", exc)
    return {}


def _get_max_body_bytes() -> int:
    settings = _get_settings()
    try:
        kb = int(settings.get("max_body_kb") or get_settings().darktrace_max_body_kb)
    except (TypeError, ValueError):
        kb = 1024
    return max(1, kb) * 1024


# Read at request time, not import time, so a secret saved in the UI takes
# effect without a restart.
# Lookup errors propagate so callers can tell "lookup failed" from "not set".
def _get_secret() -> Optional[str]:
    return get_secret("DARKTRACE_WEBHOOK_SECRET") or None


def _get_console_url() -> str:
    url = _get_settings().get("url")
    if url:
        return str(url)
    return get_settings().darktrace_url


async def _read(request: Request, signature: Optional[str], endpoint: str) -> Dict:
    return await read_signed_json(
        request,
        signature,
        endpoint=endpoint,
        vendor="Darktrace",
        header="X-Darktrace-Signature",
        secret_name="DARKTRACE_WEBHOOK_SECRET",
        get_secret=_get_secret,
        get_max_body_bytes=_get_max_body_bytes,
    )


def _ingest(
    payload: Dict,
    transform: Callable[[DarktraceIngestionService, Dict], Optional[Dict]],
    alert_type: str,
) -> Dict:
    service = DarktraceIngestionService(console_url=_get_console_url())
    return ingest(service, transform(service, payload), f"Darktrace {alert_type}")


@router.get("/health")
async def health() -> Dict:
    """Liveness probe for Darktrace's webhook test feature."""
    try:
        secret_configured = _get_secret() is not None
    except Exception:  # noqa: BLE001
        secret_configured = False
    return {
        "status": "ok",
        "receiver": "darktrace",
        "secret_configured": secret_configured,
        "rejections": rejection_counts("darktrace/"),
    }


@router.post("/model-breach", status_code=status.HTTP_202_ACCEPTED)
async def model_breach(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    payload = await _read(request, x_darktrace_signature, "darktrace/model-breach")
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_model_breach(p),
        "model-breach",
    )


@router.post("/ai-analyst", status_code=status.HTTP_202_ACCEPTED)
async def ai_analyst(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    payload = await _read(request, x_darktrace_signature, "darktrace/ai-analyst")
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_ai_analyst(p),
        "ai-analyst",
    )


@router.post("/system-status", status_code=status.HTTP_202_ACCEPTED)
async def system_status(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    payload = await _read(request, x_darktrace_signature, "darktrace/system-status")
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_system_status(p),
        "system-status",
    )
