"""check_credentials reads the saved URL and credentials, read-only, per integration."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from core.integrations import credential_check

pytestmark = pytest.mark.unit


def _resolved(monkeypatch, **fields):
    monkeypatch.setattr(credential_check, "resolve", lambda descriptor: fields)


async def test_integration_without_a_check_is_left_to_the_mcp_probe():
    assert await credential_check.check_credentials("virustotal") is None


async def test_splunk_without_a_password_says_what_is_missing(monkeypatch):
    _resolved(monkeypatch, server_url="https://s:8089", username="u", password=None)

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Missing password",
    )


async def test_splunk_reports_what_the_login_said(monkeypatch):
    _resolved(
        monkeypatch,
        server_url="https://s:8089",
        username="u",
        password="wrong",
        verify_ssl=True,
        ca_cert_path=None,
    )
    seen = {}

    def test_connection(self):
        seen["url"], seen["password"] = self.server_url, self.password
        return False, "Authentication failed"

    monkeypatch.setattr(
        "core.integrations.splunk.client.SplunkService.test_connection",
        test_connection,
    )

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Authentication failed",
    )
    assert seen == {"url": "https://s:8089", "password": "wrong"}


async def test_elastic_without_a_url_says_so(monkeypatch):
    _resolved(monkeypatch, elasticsearch_url=None)

    assert await credential_check.check_credentials("elastic-siem") == (
        False,
        "Missing elasticsearch_url",
    )


async def test_elastic_unreachable_url_fails_with_the_connection_error(monkeypatch):
    # nothing listens on port 1: a real refused connection, no mock of the client
    _resolved(
        monkeypatch,
        elasticsearch_url="http://127.0.0.1:1",
        api_key="not-a-key",
        verify_ssl=None,
    )

    ok, message = await credential_check.check_credentials("elastic-siem")

    assert ok is False
    assert message


async def test_a_check_that_blows_up_is_a_failure_that_names_no_value(monkeypatch):
    async def boom():
        raise RuntimeError("http://user:hunter2@host")

    monkeypatch.setitem(credential_check._CHECKS, "splunk", boom)

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Could not check the connection (RuntimeError)",
    )


async def test_a_check_with_no_answer_stops_at_the_deadline(monkeypatch):
    async def hang():
        await asyncio.sleep(5)

    monkeypatch.setattr(credential_check, "CHECK_TIMEOUT", 0.01)
    monkeypatch.setitem(credential_check._CHECKS, "splunk", hang)

    assert await credential_check.check_credentials("splunk") == (
        False,
        "No answer within 0.01 s",
    )


# -- OpenSearch ------------------------------------------------------------

OS_URL = "https://os.example:9200"


def _opensearch(monkeypatch, **fields):
    _resolved(
        monkeypatch,
        **{"opensearch_url": OS_URL, "username": "admin", "password": "the-secret"}
        | fields,
    )


@respx.mock
async def test_opensearch_logs_in_and_reads_the_cluster_info(monkeypatch):
    _opensearch(monkeypatch)
    root = respx.get(f"{OS_URL}/").mock(
        return_value=httpx.Response(
            200, json={"version": {"number": "2.17.0"}, "cluster_name": "soc"}
        )
    )

    assert await credential_check.check_credentials("opensearch") == (
        True,
        "Connected to OpenSearch 2.17.0 (cluster: soc)",
    )
    request = root.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")
    assert request.extensions["timeout"]["read"] == 5.0


@respx.mock
async def test_opensearch_wrong_password_fails_without_echoing_it(monkeypatch):
    _opensearch(monkeypatch)
    respx.get(f"{OS_URL}/").mock(return_value=httpx.Response(401, text="Unauthorized"))

    ok, message = await credential_check.check_credentials("opensearch")

    assert ok is False
    assert message.startswith("HTTP 401")
    assert "the-secret" not in message


async def test_opensearch_unreachable_url_fails(monkeypatch):
    # nothing listens on port 1: a real refused connection
    _opensearch(monkeypatch, opensearch_url="http://127.0.0.1:1")

    ok, message = await credential_check.check_credentials("opensearch")

    assert ok is False
    assert message


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"opensearch_url": None}, "Missing opensearch_url"),
        ({"password": None}, "Missing password"),
    ],
)
async def test_opensearch_says_what_is_missing(monkeypatch, fields, expected):
    _opensearch(monkeypatch, **fields)

    assert await credential_check.check_credentials("opensearch") == (False, expected)


# -- Microsoft Sentinel ----------------------------------------------------

_SENTINEL = {
    "tenant_id": "tid",
    "client_id": "cid",
    "client_secret": "the-secret",
    "subscription_id": "sub",
    "resource_group": "rg",
    "workspace_name": "ws",
}


class AzureError(Exception):
    pass


class HttpResponseError(AzureError):
    def __init__(self, message="", status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error = types.SimpleNamespace(code=code) if code else None


class ClientAuthenticationError(HttpResponseError):
    pass


class ServiceRequestError(AzureError):
    pass


@pytest.fixture
def azure_sdk(monkeypatch):
    """The Azure SDK is optional and not installed in CI; stub what the check imports."""
    exceptions = types.ModuleType("azure.core.exceptions")
    # Stand-ins with the real names, so messages read as they would.
    exceptions.AzureError = AzureError
    exceptions.HttpResponseError = HttpResponseError
    exceptions.ClientAuthenticationError = ClientAuthenticationError
    identity = types.ModuleType("azure.identity")
    identity.ClientSecretCredential = MagicMock()
    insights = types.ModuleType("azure.mgmt.securityinsight")
    insights.SecurityInsights = MagicMock()
    insights.SecurityInsights.return_value.incidents.list.return_value = iter([])
    for name, module in {
        "azure": types.ModuleType("azure"),
        "azure.core": types.ModuleType("azure.core"),
        "azure.core.exceptions": exceptions,
        "azure.identity": identity,
        "azure.mgmt": types.ModuleType("azure.mgmt"),
        "azure.mgmt.securityinsight": insights,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(
        credential=identity.ClientSecretCredential,
        incidents=insights.SecurityInsights.return_value.incidents,
        client=insights.SecurityInsights,
    )


async def test_sentinel_lists_one_incident_with_the_saved_app(monkeypatch, azure_sdk):
    _resolved(monkeypatch, **_SENTINEL)

    assert await credential_check.check_credentials("azure-sentinel") == (
        True,
        "Read incidents from workspace ws",
    )
    # short timeouts and no retries: the worker thread outlives the deadline
    pipeline = {"connection_timeout": 5.0, "read_timeout": 5.0, "retry_total": 0}
    azure_sdk.credential.assert_called_once_with(
        tenant_id="tid", client_id="cid", client_secret="the-secret", **pipeline
    )
    azure_sdk.client.assert_called_once_with(
        azure_sdk.credential.return_value, "sub", **pipeline
    )
    azure_sdk.incidents.list.assert_called_once_with(
        resource_group_name="rg", workspace_name="ws", top=1
    )


async def test_sentinel_rejected_secret_shows_only_the_aadsts_code(
    monkeypatch, azure_sdk
):
    _resolved(monkeypatch, **_SENTINEL)
    azure_sdk.incidents.list.side_effect = ClientAuthenticationError(
        "Authentication failed: AADSTS7000215: Invalid client secret provided "
        "for app 'cid' in tenant 'tid'."
    )

    assert await credential_check.check_credentials("azure-sentinel") == (
        False,
        "Microsoft Sentinel credential rejected by Entra ID (AADSTS7000215)",
    )


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (
            404,
            "ResourceGroupNotFound",
            "Microsoft Sentinel workspace not found (HTTP 404, "
            "ResourceGroupNotFound); check subscription_id, resource_group and "
            "workspace_name",
        ),
        (
            403,
            "AuthorizationFailed",
            "Microsoft Sentinel refused the read (HTTP 403, AuthorizationFailed)",
        ),
    ],
)
async def test_sentinel_refused_read_names_the_arm_code(
    monkeypatch, azure_sdk, status, code, expected
):
    _resolved(monkeypatch, **_SENTINEL)
    azure_sdk.incidents.list.side_effect = HttpResponseError(
        "The client 'cid' with object id 'x' does not have authorization",
        status_code=status,
        code=code,
    )

    assert await credential_check.check_credentials("azure-sentinel") == (
        False,
        expected,
    )


async def test_sentinel_unreachable_entra_is_not_blamed_on_the_secret(
    monkeypatch, azure_sdk
):
    # azure-identity raises ClientAuthenticationError, no status, for a
    # network failure too
    _resolved(monkeypatch, **_SENTINEL)
    failure = ClientAuthenticationError("Authentication failed: connection timed out")
    failure.__cause__ = ServiceRequestError("connection timed out")
    azure_sdk.incidents.list.side_effect = failure

    assert await credential_check.check_credentials("azure-sentinel") == (
        False,
        "Microsoft Sentinel could not get a token from Entra ID (ServiceRequestError)",
    )


async def test_sentinel_unreachable_fails(monkeypatch, azure_sdk):
    _resolved(monkeypatch, **_SENTINEL)
    azure_sdk.incidents.list.side_effect = ServiceRequestError("name resolution")

    assert await credential_check.check_credentials("azure-sentinel") == (
        False,
        "Microsoft Sentinel could not be reached (ServiceRequestError)",
    )


async def test_sentinel_says_what_is_missing(monkeypatch, azure_sdk):
    _resolved(
        monkeypatch, **(_SENTINEL | {"client_secret": None, "workspace_name": ""})
    )

    assert await credential_check.check_credentials("azure-sentinel") == (
        False,
        "Missing client_secret, workspace_name",
    )
    azure_sdk.credential.assert_not_called()


async def test_sentinel_without_the_sdk_says_how_to_install_it(monkeypatch):
    _resolved(monkeypatch, **_SENTINEL)
    monkeypatch.setitem(sys.modules, "azure.identity", None)

    ok, message = await credential_check.check_credentials("azure-sentinel")

    assert ok is False
    assert "pip install azure-mgmt-securityinsight azure-identity" in message


# -- AWS Security Hub ------------------------------------------------------


@pytest.fixture
def securityhub(monkeypatch):
    """boto3.client returns a real Security Hub client behind a Stubber."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "securityhub",
        region_name="eu-west-1",
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="the-secret",
    )
    stubber = Stubber(client)
    calls = []

    def fake_client(service, **kwargs):
        calls.append((service, kwargs))
        return client

    monkeypatch.setattr(boto3, "client", fake_client)
    with stubber:
        yield types.SimpleNamespace(stubber=stubber, calls=calls)


async def test_security_hub_reads_one_finding_with_the_saved_keys(
    monkeypatch, securityhub
):
    _resolved(
        monkeypatch,
        access_key_id="AKIAEXAMPLE",
        secret_access_key="the-secret",
        region="eu-west-1",
    )
    securityhub.stubber.add_response(
        "get_findings", {"Findings": []}, {"MaxResults": 1}
    )

    assert await credential_check.check_credentials("aws-security-hub") == (
        True,
        "Read AWS Security Hub findings in eu-west-1",
    )
    [(service, kwargs)] = securityhub.calls
    assert service == "securityhub"
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert kwargs["aws_secret_access_key"] == "the-secret"
    assert kwargs["config"].retries == {"total_max_attempts": 1}
    assert kwargs["config"].connect_timeout == kwargs["config"].read_timeout == 5.0
    securityhub.stubber.assert_no_pending_responses()


async def test_security_hub_without_keys_uses_the_default_credentials(
    monkeypatch, securityhub
):
    # as the adapter does: no saved keys means boto3's own credential chain
    _resolved(monkeypatch, access_key_id=None, secret_access_key=None, region=None)
    securityhub.stubber.add_response(
        "get_findings", {"Findings": []}, {"MaxResults": 1}
    )

    assert await credential_check.check_credentials("aws-security-hub") == (
        True,
        "Read AWS Security Hub findings in us-east-1 (default AWS credentials)",
    )
    [(_, kwargs)] = securityhub.calls
    assert kwargs["region_name"] == "us-east-1"
    assert "aws_access_key_id" not in kwargs


async def test_security_hub_rejected_key_shows_the_aws_error_code(
    monkeypatch, securityhub
):
    _resolved(
        monkeypatch,
        access_key_id="AKIAEXAMPLE",
        secret_access_key="the-secret",
        region="eu-west-1",
    )
    securityhub.stubber.add_client_error(
        "get_findings",
        service_error_code="UnrecognizedClientException",
        service_message="The security token included in the request is invalid.",
        http_status_code=403,
    )

    ok, message = await credential_check.check_credentials("aws-security-hub")

    assert (ok, message) == (
        False,
        "AWS Security Hub refused the read (HTTP 403, UnrecognizedClientException)",
    )


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            "endpoint",
            "AWS Security Hub in eu-west-1 could not be reached "
            "(EndpointConnectionError)",
        ),
        ("no-credentials", "No AWS credentials saved, and none found on this host"),
    ],
)
async def test_security_hub_unreachable_or_without_credentials_fails(
    monkeypatch, error, expected
):
    import boto3
    from botocore.exceptions import EndpointConnectionError, NoCredentialsError

    _resolved(
        monkeypatch, access_key_id=None, secret_access_key=None, region="eu-west-1"
    )
    client = MagicMock()
    client.get_findings.side_effect = (
        EndpointConnectionError(
            endpoint_url="https://securityhub.eu-west-1.amazonaws.com"
        )
        if error == "endpoint"
        else NoCredentialsError()
    )
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)

    assert await credential_check.check_credentials("aws-security-hub") == (
        False,
        expected,
    )


async def test_security_hub_half_a_key_pair_says_what_is_missing(monkeypatch):
    _resolved(
        monkeypatch, access_key_id="AKIAEXAMPLE", secret_access_key=None, region=None
    )

    assert await credential_check.check_credentials("aws-security-hub") == (
        False,
        "Missing secret_access_key",
    )


async def test_security_hub_without_boto3_says_how_to_install_it(monkeypatch):
    _resolved(monkeypatch, access_key_id=None, secret_access_key=None, region=None)
    monkeypatch.setitem(sys.modules, "boto3", None)

    assert await credential_check.check_credentials("aws-security-hub") == (
        False,
        "boto3 not installed. Install: pip install boto3",
    )


# -- Microsoft Defender ----------------------------------------------------

TOKEN_URL = "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
ALERTS_URL = "https://api.securitycenter.microsoft.com/api/alerts"


def _defender(monkeypatch, **fields):
    _resolved(
        monkeypatch,
        **{"tenant_id": "tid", "client_id": "cid", "client_secret": "the-secret"}
        | fields,
    )


@respx.mock
async def test_defender_gets_a_token_and_reads_one_alert(monkeypatch):
    _defender(monkeypatch)
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    alerts = respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    assert await credential_check.check_credentials("microsoft-defender") == (
        True,
        "Read Microsoft Defender alerts",
    )
    assert b"scope=https%3A%2F%2Fapi.securitycenter.microsoft.com%2F.default" in (
        token.calls.last.request.content
    )
    assert token.calls.last.request.extensions["timeout"]["read"] == 5.0
    request = alerts.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.url.params["$top"] == "1"
    assert request.extensions["timeout"]["read"] == 5.0


@respx.mock
async def test_defender_rejected_secret_shows_only_the_aadsts_code(monkeypatch):
    _defender(monkeypatch)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            },
        )
    )

    ok, message = await credential_check.check_credentials("microsoft-defender")

    assert (ok, message) == (
        False,
        "Microsoft Defender credential rejected by Entra ID (HTTP 401, AADSTS7000215)",
    )


@respx.mock
async def test_defender_token_without_the_alert_permission_says_so(monkeypatch):
    _defender(monkeypatch)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(403))

    assert await credential_check.check_credentials("microsoft-defender") == (
        False,
        "Microsoft Defender API refused the token (HTTP 403); the app needs the "
        "Alert.Read.All application permission",
    )


@respx.mock
async def test_defender_unreachable_fails(monkeypatch):
    _defender(monkeypatch)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    respx.get(ALERTS_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert await credential_check.check_credentials("microsoft-defender") == (
        False,
        "Microsoft Defender API could not be reached (ConnectError)",
    )


@respx.mock
async def test_defender_entra_unreachable_fails(monkeypatch):
    _defender(monkeypatch)
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))

    ok, message = await credential_check.check_credentials("microsoft-defender")

    assert ok is False
    assert "Entra ID could not be reached" in message


async def test_defender_says_what_is_missing(monkeypatch):
    _defender(monkeypatch, tenant_id=None, client_secret="")

    assert await credential_check.check_credentials("microsoft-defender") == (
        False,
        "Missing tenant_id, client_secret",
    )


# -- CrowdStrike -----------------------------------------------------------

CS_BASE = "https://api.us-2.crowdstrike.com"


def _crowdstrike(monkeypatch, **fields):
    _resolved(
        monkeypatch,
        **{"client_id": "cid", "client_secret": "the-secret", "base_url": CS_BASE}
        | fields,
    )


@respx.mock
async def test_crowdstrike_gets_a_token_and_queries_one_detection(monkeypatch):
    _crowdstrike(monkeypatch)
    respx.post(f"{CS_BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            201, json={"access_token": "tok", "expires_in": 1800}
        )
    )
    query = respx.get(f"{CS_BASE}/detects/queries/detects/v1").mock(
        return_value=httpx.Response(
            200, json={"resources": ["d1"], "meta": {"pagination": {"total": 40}}}
        )
    )

    assert await credential_check.check_credentials("crowdstrike") == (
        True,
        "Read CrowdStrike detections",
    )
    assert query.call_count == 1
    request = query.calls.last.request
    assert request.url.params["limit"] == "1"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.extensions["timeout"]["read"] == 5.0


@respx.mock
async def test_crowdstrike_rejected_client_fails_without_echoing_it(monkeypatch):
    _crowdstrike(monkeypatch)
    respx.post(f"{CS_BASE}/oauth2/token").mock(return_value=httpx.Response(401))

    ok, message = await credential_check.check_credentials("crowdstrike")

    assert (ok, message) == (False, "CrowdStrike authentication failed: HTTP 401")


@respx.mock
async def test_crowdstrike_client_without_detections_scope_fails(monkeypatch):
    _crowdstrike(monkeypatch)
    respx.post(f"{CS_BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            201, json={"access_token": "tok", "expires_in": 1800}
        )
    )
    respx.get(f"{CS_BASE}/detects/queries/detects/v1").mock(
        return_value=httpx.Response(403)
    )

    assert await credential_check.check_credentials("crowdstrike") == (
        False,
        "CrowdStrike detections query: HTTP 403",
    )


@respx.mock
async def test_crowdstrike_unreachable_fails(monkeypatch):
    _crowdstrike(monkeypatch)
    respx.post(f"{CS_BASE}/oauth2/token").mock(
        side_effect=httpx.ConnectError("refused")
    )

    ok, message = await credential_check.check_credentials("crowdstrike")

    assert ok is False
    assert message.startswith("CrowdStrike authentication error")
    assert "the-secret" not in message


async def test_crowdstrike_says_what_is_missing(monkeypatch):
    _crowdstrike(monkeypatch, client_secret=None)

    assert await credential_check.check_credentials("crowdstrike") == (
        False,
        "Missing client_secret",
    )
