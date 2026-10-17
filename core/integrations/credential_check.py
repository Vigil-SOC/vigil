"""One read-only check that a saved integration's URL and credentials work.

The MCP probe behind "Test" only shows that a server answered. A server starts
and lists its tools whatever it is pointed at, so a wrong password or an
unreachable URL still read as Good. This logs in with the saved settings and
makes one small read, and nothing else: the server's own info endpoint, or a
list of at most one alert. Nothing is written to the external system.

Each source that federation pulls alerts from has a check, built on the client
and credentials its adapter uses. For any other id ``check_credentials``
returns ``None``, and the test stays what the MCP probe said.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

import httpx

from core.integrations._base.config import missing, resolve
from core.integrations.aws_security_hub.descriptor import AWS_SECURITY_HUB
from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.integrations.elastic.descriptor import ELASTIC
from core.integrations.microsoft_defender.descriptor import MICROSOFT_DEFENDER
from core.integrations.opensearch.descriptor import OPENSEARCH
from core.integrations.splunk.descriptor import SPLUNK

logger = logging.getLogger(__name__)

# (success, one line safe to show: never a secret)
Outcome = Tuple[bool, str]

# The UI's Test request gives up after 15 s and the MCP probe runs first, so
# the answer is not waited for longer than this. A sync client keeps running in
# its worker thread after it, until its own timeouts end it: hence the short
# per-request timeouts and no retries below.
CHECK_TIMEOUT = 10.0
_REQUEST_TIMEOUT = 5.0

# The host the Defender adapter reads alerts from, and its token audience.
_DEFENDER_API = "https://api.securitycenter.microsoft.com"

_AADSTS = re.compile(r"AADSTS\d+")


def _absent(names: Tuple[str, ...]) -> Outcome:
    return False, f"Missing {', '.join(names)}"


def _half_set(config: Mapping[str, Any], *names: str) -> Tuple[str, ...]:
    """The empty ones of fields that only work together, once one is set."""
    absent = missing(config, *names)
    return absent if len(absent) < len(names) else ()


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


async def _opensearch() -> Outcome:
    from core.integrations.opensearch.client import OpenSearchService

    config = resolve(OPENSEARCH)
    if missing(config, "opensearch_url"):
        return _absent(("opensearch_url",))
    # A cluster without the security plugin takes no login; half of one is a
    # mistake that would otherwise read as an anonymous 401.
    absent = _half_set(config, "username", "password")
    if absent:
        return _absent(absent)
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = OpenSearchService(
        opensearch_url=config["opensearch_url"],
        dashboards_url=config.get("dashboards_url"),
        username=config.get("username"),
        password=config.get("password"),
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
        timeout=_REQUEST_TIMEOUT,
    )
    try:
        # GET / on OpenSearch (and /api/status on Dashboards when one is set)
        return await service.test_connection()
    finally:
        await service.close()


async def _sentinel() -> Outcome:
    config = resolve(AZURE_SENTINEL)
    absent = missing(
        config,
        "tenant_id",
        "client_id",
        "client_secret",
        "subscription_id",
        "resource_group",
        "workspace_name",
    )
    if absent:
        return _absent(absent)
    try:
        from azure.core.exceptions import (
            AzureError,
            ClientAuthenticationError,
            HttpResponseError,
        )
        from azure.identity import ClientSecretCredential
        from azure.mgmt.securityinsight import SecurityInsights
    except ImportError:
        return False, (
            "Azure SDK not installed. Install: pip install "
            "azure-mgmt-securityinsight azure-identity"
        )

    # azure-core's defaults are 300 s to connect and to read, and 10 retries.
    pipeline = {
        "connection_timeout": _REQUEST_TIMEOUT,
        "read_timeout": _REQUEST_TIMEOUT,
        "retry_total": 0,
    }

    def read() -> None:
        # The client the Sentinel adapter builds, asked for one incident: a
        # token from Entra ID, then one read of the workspace.
        credential = ClientSecretCredential(
            tenant_id=config["tenant_id"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            **pipeline,
        )
        client = SecurityInsights(credential, config["subscription_id"], **pipeline)
        try:
            incidents = client.incidents.list(
                resource_group_name=config["resource_group"],
                workspace_name=config["workspace_name"],
                top=1,
            )
            next(iter(incidents), None)
        finally:
            client.close()
            credential.close()

    try:
        await asyncio.to_thread(read)
    except HttpResponseError as exc:
        # Both messages name the app and tenant; only the codes are shown.
        if isinstance(exc, ClientAuthenticationError):
            aadsts = _AADSTS.search(str(exc))
            if aadsts:
                return False, (
                    "Microsoft Sentinel credential rejected by Entra ID "
                    f"({aadsts.group(0)})"
                )
            if not exc.status_code:
                # azure-identity wraps a network failure the same way
                cause = type(exc.__cause__ or exc).__name__
                return False, (
                    f"Microsoft Sentinel could not get a token from Entra ID ({cause})"
                )
        code = getattr(getattr(exc, "error", None), "code", None)
        detail = f"HTTP {exc.status_code}" + (f", {code}" if code else "")
        if exc.status_code == 404:
            return False, (
                f"Microsoft Sentinel workspace not found ({detail}); check "
                "subscription_id, resource_group and workspace_name"
            )
        return False, f"Microsoft Sentinel refused the read ({detail})"
    except AzureError as exc:
        return False, f"Microsoft Sentinel could not be reached ({type(exc).__name__})"
    return True, f"Read incidents from workspace {config['workspace_name']}"


async def _security_hub() -> Outcome:
    config = resolve(AWS_SECURITY_HUB)
    absent = _half_set(config, "access_key_id", "secret_access_key")
    if absent:
        return _absent(absent)
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return False, "boto3 not installed. Install: pip install boto3"

    region = config.get("region") or "us-east-1"
    # No keys saved: the default AWS credentials, as the adapter falls back to.
    keys = (
        {
            "aws_access_key_id": config["access_key_id"],
            "aws_secret_access_key": config["secret_access_key"],
        }
        if config.get("access_key_id")
        else {}
    )

    def read() -> None:
        client = boto3.client(
            "securityhub",
            region_name=region,
            config=Config(
                connect_timeout=_REQUEST_TIMEOUT,
                read_timeout=_REQUEST_TIMEOUT,
                retries={"total_max_attempts": 1},
            ),
            **keys,
        )
        # the adapter's own call, for one finding
        client.get_findings(MaxResults=1)

    try:
        await asyncio.to_thread(read)
    except NoCredentialsError:
        return False, "No AWS credentials saved, and none found on this host"
    except ClientError as exc:
        # The code says what is wrong (UnrecognizedClientException,
        # AccessDeniedException, InvalidAccessException when the Hub is off).
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return False, (
            f"AWS Security Hub refused the read "
            f"(HTTP {status}, {error.get('Code') or 'no error code'})"
        )
    except BotoCoreError as exc:
        return False, (
            f"AWS Security Hub in {region} could not be reached ({type(exc).__name__})"
        )
    source = "" if keys else " (default AWS credentials)"
    return True, f"Read AWS Security Hub findings in {region}{source}"


async def _defender() -> Outcome:
    from core.integrations._base.aad_token import (
        TokenError,
        fetch_client_credentials_token,
    )

    config = resolve(MICROSOFT_DEFENDER)
    absent = missing(config, "tenant_id", "client_id", "client_secret")
    if absent:
        return _absent(absent)

    def read() -> httpx.Response:
        # The token the Defender server uses, then the adapter's alert list
        # for one alert.
        token = fetch_client_credentials_token(
            "Microsoft Defender",
            config["tenant_id"],
            config["client_id"],
            config["client_secret"],
            f"{_DEFENDER_API}/.default",
            timeout=_REQUEST_TIMEOUT,
        )
        return httpx.get(
            f"{_DEFENDER_API}/api/alerts",
            headers={"Authorization": f"Bearer {token}"},
            params={"$top": 1},
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
        )

    try:
        resp = await asyncio.to_thread(read)
    except TokenError as exc:
        return False, str(exc)
    except httpx.HTTPError as exc:
        return False, (
            f"Microsoft Defender API could not be reached ({type(exc).__name__})"
        )
    if resp.status_code in (401, 403):
        return False, (
            f"Microsoft Defender API refused the token (HTTP {resp.status_code}); "
            "the app needs the Alert.Read.All application permission"
        )
    if resp.status_code != 200:
        return False, f"Microsoft Defender API answered HTTP {resp.status_code}"
    return True, "Read Microsoft Defender alerts"


async def _crowdstrike() -> Outcome:
    from core.integrations.crowdstrike.client import CrowdStrikeService

    config = resolve(CROWDSTRIKE)
    absent = missing(config, "client_id", "client_secret")
    if absent:
        return _absent(absent)
    service = CrowdStrikeService(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        base_url=config.get("base_url") or "https://api.crowdstrike.com",
        timeout=_REQUEST_TIMEOUT,
    )

    def read() -> Outcome:
        # The adapter's token exchange, then its detections ID query for one
        # ID. last_error is a status or a connection error, never the secret.
        if not service._ensure_authenticated():
            return False, f"CrowdStrike {service.last_error or 'authentication failed'}"
        if service._query_detection_ids(None, 1) is None:
            return False, f"CrowdStrike {service.last_error}"
        return True, "Read CrowdStrike detections"

    try:
        return await asyncio.to_thread(read)
    finally:
        service.session.close()


_CHECKS: Dict[str, Callable[[], Awaitable[Outcome]]] = {
    SPLUNK.id: _splunk,
    ELASTIC.id: _elastic,
    OPENSEARCH.id: _opensearch,
    AZURE_SENTINEL.id: _sentinel,
    AWS_SECURITY_HUB.id: _security_hub,
    MICROSOFT_DEFENDER.id: _defender,
    CROWDSTRIKE.id: _crowdstrike,
}


async def check_credentials(integration_id: str) -> Optional[Outcome]:
    """``(ok, message)`` for the saved settings, or ``None`` when no check exists."""
    check = _CHECKS.get(integration_id)
    if check is None:
        return None
    try:
        return await asyncio.wait_for(check(), CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Credential check for %s timed out", integration_id)
        return False, f"No answer within {CHECK_TIMEOUT:g} s"
    except Exception as exc:  # noqa: BLE001
        # The class name only: a client error can carry the URL it was given.
        logger.warning(
            "Credential check for %s failed to run (%s)",
            integration_id,
            type(exc).__name__,
        )
        return False, f"Could not check the connection ({type(exc).__name__})"
