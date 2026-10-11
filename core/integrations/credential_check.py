"""One read-only check that a saved integration's URL and credentials work.

The MCP probe behind "Test" only shows that a server answered. A Splunk or
Elastic server starts and lists its tools whatever it is pointed at, so a wrong
password or an unreachable URL still read as Good. This logs in and reads the
server's own info endpoint with the saved settings, and nothing else: no search,
no write to the external system.

Only integrations with a client that can answer are listed. For any other id
``check_credentials`` returns ``None``, and the test stays what the MCP probe
said.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional, Tuple

from core.integrations._base.config import missing, resolve
from core.integrations.elastic.descriptor import ELASTIC
from core.integrations.splunk.descriptor import SPLUNK

logger = logging.getLogger(__name__)

# (success, one line safe to show: never a secret)
Outcome = Tuple[bool, str]


def _absent(names: Tuple[str, ...]) -> Outcome:
    return False, f"Missing {', '.join(names)}"


async def _splunk() -> Outcome:
    from core.integrations.splunk.client import SplunkService

    config = resolve(SPLUNK)
    absent = missing(config, "server_url", "username", "password")
    if absent:
        return _absent(absent)
    service = SplunkService(
        server_url=config["server_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=config["verify_ssl"],
        ca_cert_path=config.get("ca_cert_path"),
    )
    try:
        # sync client: login, then GET /services/server/info
        return await asyncio.to_thread(service.test_connection)
    finally:
        if service._session is not None:
            service._session.close()


async def _elastic() -> Outcome:
    from core.integrations.elastic.client import ElasticService

    config = resolve(ELASTIC)
    if missing(config, "elasticsearch_url"):
        return _absent(("elasticsearch_url",))
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = ElasticService(
        elasticsearch_url=config["elasticsearch_url"],
        kibana_url=config.get("kibana_url"),
        api_key=config.get("api_key"),
        username=config.get("username"),
        password=config.get("password"),
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )
    try:
        # GET / on Elasticsearch (and /api/status on Kibana when one is set)
        return await service.test_connection()
    finally:
        await service.close()


_CHECKS: Dict[str, Callable[[], Awaitable[Outcome]]] = {
    SPLUNK.id: _splunk,
    ELASTIC.id: _elastic,
}


async def check_credentials(integration_id: str) -> Optional[Outcome]:
    """``(ok, message)`` for the saved settings, or ``None`` when no check exists."""
    check = _CHECKS.get(integration_id)
    if check is None:
        return None
    try:
        return await check()
    except Exception as exc:  # noqa: BLE001
        # The class name only: a client error can carry the URL it was given.
        logger.warning(
            "Credential check for %s failed to run (%s)",
            integration_id,
            type(exc).__name__,
        )
        return False, f"Could not check the connection ({type(exc).__name__})"
