"""One read-only check that a saved integration's URL and credentials work.

The MCP probe behind "Test" only shows that a server answered. A server starts
and lists its tools whatever it is pointed at, so a wrong password or an
unreachable URL still read as Good. This logs in with the saved settings and
makes one small read, and nothing else: the server's own info endpoint, a
list of at most one alert, or one fixed read-only MCP call. Nothing is written
to the external system.

Each source that federation pulls alerts from has a direct check, built on the
client and credentials its adapter uses. Anything else with a read-only call
is one row in ``_READ_ONLY``: a fixed tool and fixed arguments, called through
``execute_mcp_tool``. An id in ``_NO_READ_ONLY``, or a fixed call with no
credential saved, is not checked: Test says so and stores nothing, and the
judge does not withhold readiness. Anything left returns ``None``, and the
test stays what the MCP probe said.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple, Union

import httpx

from core.integrations._base.config import missing, resolve, resolve_fields
from core.integrations.amazon_security_lake.descriptor import AMAZON_SECURITY_LAKE
from core.integrations.aws_security_hub.descriptor import AWS_SECURITY_HUB
from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.integrations.elastic.descriptor import ELASTIC
from core.integrations.microsoft_defender.descriptor import MICROSOFT_DEFENDER
from core.integrations.opencti.descriptor import OPENCTI, OPENCTI_REQUIRED
from core.integrations.opensearch.descriptor import OPENSEARCH
from core.integrations.splunk.descriptor import SPLUNK
from core.integrations.wazuh.descriptor import WAZUH, WAZUH_REQUIRED

logger = logging.getLogger(__name__)

# (success, one line safe to show: never a secret)
Outcome = Tuple[bool, str]


@dataclass(frozen=True)
class NotChecked:
    """The check did not run. ``reason`` is one fixed line, safe to show."""

    reason: str


Check = Union[Outcome, NotChecked]

# No network call exists, so Test and the health judge say so instead of
# "no real check". #4872 adds ids here; the reason is the judge's text.
_NO_READ_ONLY: Dict[str, str] = {
    "url-analysis": "no read-only call exists",
    # The only tool each lists writes (teams_send_alert, atomic_red_team_execute).
    "microsoft-teams": "no read-only call exists",
    "atomic-red-team": "no read-only call exists",
}

# A fixed call with no saved credential is the same outcome. Jira signs in
# through Atlassian's OAuth, not the stored api_token.
_NO_CREDENTIAL_UNCHECKED = frozenset(
    {
        "sentinelone",
        "carbon-black",
        "okta",
        "azure-ad",
        "palo-alto",
        "cloudflare",
        "slack",
        "pagerduty",
        "vstrike",
    }
)

# A public address and an empty file. Lookups of these prove the credential
# without asking about a customer indicator.
_BENIGN_IP = "8.8.8.8"
_BENIGN_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

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


async def _wazuh() -> Outcome:
    from core.integrations.wazuh.client import WazuhService

    config = resolve(WAZUH)
    absent = missing(config, *WAZUH_REQUIRED)
    if absent:
        return _absent(absent)
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = WazuhService(
        api_url=config["api_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
        timeout=_REQUEST_TIMEOUT,
    )
    try:
        # JWT login, then GET / on the manager API. A 401 and a connect error
        # come back as their own messages.
        return await service.test_connection()
    finally:
        await service.close()


async def _opencti() -> Outcome:
    from core.integrations.opencti.client import OpenCTIService

    config = resolve(OPENCTI)
    absent = missing(config, *OPENCTI_REQUIRED)
    if absent:
        return _absent(absent)
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = OpenCTIService(
        url=config["url"],
        api_token=config["api_token"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
        timeout=_REQUEST_TIMEOUT,
    )
    try:
        # about { version } with the token: a bad token and a wrong URL differ.
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


def _env_keys() -> bool:
    import boto3

    # Only the chain's first link (method "env"): walking on would probe IMDS
    # twice, here and again in discovery.
    resolver = boto3.Session()._session.get_component("credential_provider")
    return resolver.get_provider("env").load() is not None


async def _security_lake() -> Outcome:
    from core.integrations.amazon_security_lake import lake

    config = resolve(AMAZON_SECURITY_LAKE)
    # Static keys in this backend's environment win its chain and are never
    # forwarded to the server: whatever they can read, the server cannot.
    # Checked on the base chain, not the session a role_arn wraps as "assume-role".
    if await asyncio.to_thread(_env_keys):
        return False, (
            "This backend signs in to AWS with static keys (AWS_ACCESS_KEY_ID), "
            "which the Security Lake server never receives: give it a role "
            "(instance role, IRSA, Pod Identity) or a mounted shared config"
        )
    # The server's own discovery, every region at once, inside CHECK_TIMEOUT. It
    # runs on this backend's AWS chain, which must match the runner's.
    found = await asyncio.to_thread(
        lake.discover, lambda: lake.build_session(config), CHECK_TIMEOUT - 2.5
    )
    return lake.summary(found, lake.parse_regions(config.get("allowed_regions")))


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


async def _cloudforce_one() -> Outcome:
    # No descriptor and no MCP server: the poller reads the same fields. The
    # token is a secret, so it is not in the stored config dict.
    config = resolve_fields(
        "cloudforce_one", ("api_token", "taxii_server_url", "collection_ids")
    )
    collection_ids = [
        part.strip()
        for part in str(config.get("collection_ids") or "").split(",")
        if part.strip()
    ]
    absent = missing(config, "api_token", "taxii_server_url")
    if not collection_ids:
        absent = (*absent, "collection_ids")
    if absent:
        return _absent(absent)

    def discover() -> Outcome:
        from taxii2client.v21 import Server

        from core.threat_intel.threat_feed_service import _BearerAuth

        # Discovery only. Fetching the collection pages every object.
        # Each API root opens its own session, so the short timeout has to be
        # put on that session before its collections are listed.
        server = Server(
            str(config["taxii_server_url"]),
            auth=_BearerAuth(str(config["api_token"])),
        )

        def limit(session: Any) -> None:
            request = session.request

            def limited(method: str, url: str, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
                return request(method, url, **kwargs)

            session.request = limited

        roots: list = []
        try:
            limit(server._conn.session)
            roots = list(server.api_roots)
            wanted = collection_ids[0]
            for root in roots:
                limit(root._conn.session)
                for collection in root.collections:
                    if collection.id == wanted:
                        return True, "Cloudforce One accepted the call"
            return False, "Cloudforce One collection was not found"
        finally:
            server.close()
            for root in roots:
                root.close()

    try:
        return await asyncio.to_thread(discover)
    except Exception as exc:  # noqa: BLE001
        # The class name only: the HTTP error names the URL it was given.
        logger.warning(
            "Credential check for cloudforce_one failed (%s)", type(exc).__name__
        )
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return False, "Cloudforce One rejected the call"
        return False, "Cloudforce One could not be reached"


_CHECKS: Dict[str, Callable[[], Awaitable[Outcome]]] = {
    SPLUNK.id: _splunk,
    ELASTIC.id: _elastic,
    OPENSEARCH.id: _opensearch,
    WAZUH.id: _wazuh,
    OPENCTI.id: _opencti,
    AZURE_SENTINEL.id: _sentinel,
    AWS_SECURITY_HUB.id: _security_hub,
    AMAZON_SECURITY_LAKE.id: _security_lake,
    MICROSOFT_DEFENDER.id: _defender,
    CROWDSTRIKE.id: _crowdstrike,
    "cloudforce_one": _cloudforce_one,
}


@dataclass(frozen=True)
class FixedCall:
    """One fixed read. ``tool`` is the name the server lists, not the flat name."""

    server: str
    tool: str
    args: Mapping[str, Any]
    label: str


# Each call is the cheapest read that still needs the credential: an account
# or quota call where the server has one, otherwise one benign lookup.
# 192.0.2.1 is TEST-NET-1 (RFC 5737). Taken from a live tools/list, except
# Jira: its remote server does not list tools until the OAuth sign-in, and
# atlassianUserInfo is that server's read of the account.
_READ_ONLY: Dict[str, FixedCall] = {
    # relationships=[] is one GET. The default fans out into premium lookups.
    "virustotal": FixedCall(
        "virustotal",
        "get_domain_report",
        {"domain": "google.com", "relationships": []},
        "VirusTotal",
    ),
    "shodan": FixedCall("shodan", "api_info", {}, "Shodan"),
    "firecrawl": FixedCall(
        "firecrawl",
        "firecrawl_scrape",
        {"url": "https://example.com", "formats": ["markdown"]},
        "Firecrawl",
    ),
    "alienvault-otx": FixedCall(
        "alienvault-otx", "otx_check_ip", {"ip": _BENIGN_IP}, "AlienVault OTX"
    ),
    "misp": FixedCall("misp", "misp_get_events", {"limit": 1}, "MISP"),
    "gcp-threat-intel": FixedCall(
        "gcp-threat-intel",
        "list_threat_profiles",
        {"limit": 1},
        "Google Threat Intelligence",
    ),
    "ip-geolocation": FixedCall(
        "ip-geolocation", "geolocate_ip", {"ip": _BENIGN_IP}, "IP Geolocation"
    ),
    "hybrid-analysis": FixedCall(
        "hybrid-analysis", "ha_search_hash", {"hash": _BENIGN_SHA256}, "Hybrid Analysis"
    ),
    "joe-sandbox": FixedCall(
        "joe-sandbox", "get_list_of_recent_analyses", {"limit": 1}, "Joe Sandbox"
    ),
    "anyrun": FixedCall(
        "anyrun", "anyrun_search_hash", {"hash": _BENIGN_SHA256}, "ANY.RUN"
    ),
    "cape-sandbox": FixedCall(
        "cape-sandbox", "cape_list_tasks", {"limit": 1}, "CAPE Sandbox"
    ),
    "sentinelone": FixedCall("sentinelone", "list_alerts", {"first": 1}, "SentinelOne"),
    "carbon-black": FixedCall(
        "carbon-black", "cb_get_alerts", {"limit": 1}, "Carbon Black"
    ),
    "okta": FixedCall("okta", "list_okta_users", {"max_results": 1}, "Okta"),
    "azure-ad": FixedCall("azure-ad", "aad_get_sign_ins", {"limit": 1}, "Entra"),
    "palo-alto": FixedCall("palo-alto", "pan_get_threats", {"limit": 1}, "Palo Alto"),
    "cloudflare": FixedCall(
        "cloudflare",
        "cf_lookup_ip_threat",
        {"ip": "192.0.2.1"},
        "Cloudflare",
    ),
    "jira": FixedCall("jira", "atlassianUserInfo", {}, "Jira"),
    "slack": FixedCall("slack", "slack_auth_test", {}, "Slack"),
    "pagerduty": FixedCall("pagerduty", "list_incidents", {"limit": 1}, "PagerDuty"),
    "vstrike": FixedCall("vstrike", "vstrike_storyline_list", {}, "VStrike"),
}


def _timeout_message() -> str:
    return f"No answer within {CHECK_TIMEOUT:g} s"


def _rejected(label: str) -> Outcome:
    return False, f"{label} rejected the call"


def _from_connect(kind: str, label: str) -> Check:
    if kind == "not_enabled":
        return NotChecked("not checked: tool server is not enabled")
    if kind == "not_configured":
        return NotChecked("not checked: tool server is not configured")
    if kind == "timeout":
        return False, _timeout_message()
    if kind == "no_client":
        return False, "Could not check the connection (no MCP client)"
    return False, f"{label} could not be reached"


def _from_mcp(label: str, kind: str) -> Outcome:
    from core.agents.mcp_tools import TIMEOUT, UNAVAILABLE

    if kind == TIMEOUT:
        return False, _timeout_message()
    if kind == UNAVAILABLE:
        return False, f"{label} could not be reached"
    return _rejected(label)


def _no_credential(integration_id: str) -> bool:
    """True when a fixed call has nothing saved to authenticate with."""
    from core.integrations._base.descriptor import get_descriptor

    if integration_id == "jira":
        return False
    desc = get_descriptor(integration_id)
    if desc is None or not desc.secret_fields:
        return desc is not None and not desc.secret_fields
    try:
        config = resolve(desc)
    except Exception as exc:  # noqa: BLE001 - a failed read is not "no credential"
        logger.warning(
            "Could not read credentials for %s (%s)",
            integration_id,
            type(exc).__name__,
        )
        return False
    return all(not config.get(name) for name in desc.secret_fields)


def not_checked_reason(integration_id: str) -> Optional[str]:
    """Why this id is not checked, or ``None`` when a check exists or might."""
    reason = _NO_READ_ONLY.get(integration_id)
    if reason:
        return reason
    if integration_id in _NO_CREDENTIAL_UNCHECKED and _no_credential(integration_id):
        return "no read-only call exists"
    return None


async def _mcp_read(integration_id: str) -> Check:
    """One fixed call. Connecting and the call share ``CHECK_TIMEOUT``."""
    from core.agents.mcp_tools import MCPFailure, execute_mcp_tool, row_error
    from core.integrations.entity_facts import Source, _connect

    call = _READ_ONLY[integration_id]
    deadline = time.monotonic() + CHECK_TIMEOUT
    registry, reason = await _connect(Source(call.server, "", (), ()), deadline)
    if registry is None:
        # ``reason`` can quote the server. Only its kind is kept, and a server
        # that is off is an expected "not checked", not a failed check.
        kind = reason[0]
        if kind not in ("not_enabled", "not_configured"):
            logger.warning(
                "Credential check for %s did not connect (%s)", integration_id, kind
            )
        return _from_connect(kind, call.label)
    try:
        rows, handled = await execute_mcp_tool(
            f"{call.server}_{call.tool}",
            dict(call.args),
            max(0.1, deadline - time.monotonic()),
            registry,
        )
    except MCPFailure as exc:
        logger.warning("Credential check for %s failed (%s)", integration_id, exc.kind)
        return _from_mcp(call.label, exc.kind)
    if not handled:
        return NotChecked("not checked: the read-only call is not available")
    # An error row, or Okta's text-only "Error executing tool" reply.
    if row_error(rows):
        logger.warning("Credential check for %s failed (error)", integration_id)
        return _rejected(call.label)
    return True, f"{call.label} accepted the call"


async def check_credentials(integration_id: str) -> Optional[Check]:
    """``(ok, message)`` for the saved settings, ``NotChecked`` when nothing
    ran, or ``None`` when no check exists."""
    skipped = not_checked_reason(integration_id)
    if skipped is not None:
        return NotChecked(f"not checked: {skipped}")
    if integration_id not in _CHECKS and integration_id not in _READ_ONLY:
        return None

    async def run() -> Check:
        if integration_id in _CHECKS:
            return await _CHECKS[integration_id]()
        return await _mcp_read(integration_id)

    try:
        return await asyncio.wait_for(run(), CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Credential check for %s timed out", integration_id)
        return False, _timeout_message()
    except Exception as exc:  # noqa: BLE001
        # The class name only: a client error can carry the URL it was given.
        logger.warning(
            "Credential check for %s failed to run (%s)",
            integration_id,
            type(exc).__name__,
        )
        return False, f"Could not check the connection ({type(exc).__name__})"


def has_check(integration_id: str) -> bool:
    """True when Test makes a real read for this integration."""
    return integration_id in _CHECKS or integration_id in _READ_ONLY
