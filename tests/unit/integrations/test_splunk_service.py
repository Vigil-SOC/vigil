"""Unit tests for services/splunk_service.py (httpx transport, respx-mocked).

The module had no HTTP-level coverage at all, which is how it shipped with
no timeout on any request — a hang against an unresponsive Splunk was
unbounded, and search() is driven by the daemon's poll loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.integrations.splunk.client import SplunkService  # noqa: E402

BASE = "https://splunk.example.com:8089"


def _service(**kwargs) -> SplunkService:
    return SplunkService(
        server_url=kwargs.get("server_url", BASE),
        username=kwargs.get("username", "svc_vigil"),
        password=kwargs.get("password", "secret"),
        verify_ssl=kwargs.get("verify_ssl", False),
    )


def _mock_auth() -> None:
    respx.post(f"{BASE}/services/auth/login").mock(
        return_value=httpx.Response(200, json={"sessionKey": "sk-1"})
    )


# --------------------------------------------------------------------- #
# Client configuration — the actual defect this change fixes
# --------------------------------------------------------------------- #


def test_client_has_an_explicit_timeout_budget():
    """Every request must be bounded.

    requests defaulted to timeout=None and not one call site passed one,
    so an unresponsive Splunk hung the caller forever.
    """
    svc = _service()
    timeout = svc.session.timeout

    assert timeout.connect == 10.0
    assert timeout.read == 60.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0


def test_client_follows_redirects_like_requests_did():
    """httpx defaults this to False; requests followed redirects."""
    assert _service().session.follow_redirects is True


def test_verify_ssl_is_applied_at_construction():
    """httpx.Client takes verify only in the constructor.

    requests.Session allowed `session.verify = x` afterwards, which is what
    this module used to do — that assignment silently does nothing on httpx,
    so a verify_ssl=False deployment would start failing on self-signed
    certs. Constructing both ways must not raise.
    """
    assert _service(verify_ssl=False) is not None
    assert _service(verify_ssl=True) is not None


# --------------------------------------------------------------------- #
# Transport behaviour
# --------------------------------------------------------------------- #


@respx.mock
def test_authenticate_stores_session_key_and_auth_header():
    _mock_auth()
    svc = _service()

    assert svc.authenticate() is True
    assert svc.session_key == "sk-1"
    assert svc.session.headers["Authorization"] == "Splunk sk-1"


@respx.mock
def test_test_connection_success():
    _mock_auth()
    respx.get(f"{BASE}/services/server/info").mock(
        return_value=httpx.Response(200, json={"entry": []})
    )

    ok, message = _service().test_connection()
    assert ok is True, message


@respx.mock
def test_test_connection_reports_network_error_without_raising():
    """httpx.ConnectError must be caught where RequestException was."""
    respx.post(f"{BASE}/services/auth/login").mock(
        side_effect=httpx.ConnectError("refused")
    )

    ok, message = _service().test_connection()
    assert ok is False
    # authenticate() swallows the error and reports a failed auth.
    assert message


@respx.mock
def test_test_connection_follows_redirects():
    _mock_auth()
    respx.get(f"{BASE}/services/server/info").mock(
        return_value=httpx.Response(
            307, headers={"Location": f"{BASE}/services/server/info/"}
        )
    )
    respx.get(f"{BASE}/services/server/info/").mock(
        return_value=httpx.Response(200, json={"entry": []})
    )

    ok, message = _service().test_connection()
    assert ok is True, message


@respx.mock
def test_search_runs_the_full_job_lifecycle():
    """create job -> poll status -> fetch results -> delete job."""
    _mock_auth()
    job_url = f"{BASE}/services/search/jobs"
    respx.post(job_url).mock(return_value=httpx.Response(201, json={"sid": "sid-1"}))
    respx.get(f"{job_url}/sid-1").mock(
        return_value=httpx.Response(
            200, json={"entry": [{"content": {"isDone": True}}]}
        )
    )
    respx.get(f"{job_url}/sid-1/results").mock(
        return_value=httpx.Response(200, json={"results": [{"_raw": "event-1"}]})
    )
    delete_route = respx.delete(f"{job_url}/sid-1").mock(
        return_value=httpx.Response(200)
    )

    results = _service().search("index=notable", max_count=10)

    assert results == [{"_raw": "event-1"}]
    assert delete_route.called, "completed search job should be cleaned up"


@respx.mock
def test_search_sends_the_spl_query_as_form_data():
    _mock_auth()
    job_url = f"{BASE}/services/search/jobs"
    create = respx.post(job_url).mock(
        return_value=httpx.Response(201, json={"sid": "sid-1"})
    )
    respx.get(f"{job_url}/sid-1").mock(
        return_value=httpx.Response(
            200, json={"entry": [{"content": {"isDone": True}}]}
        )
    )
    respx.get(f"{job_url}/sid-1/results").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.delete(f"{job_url}/sid-1").mock(return_value=httpx.Response(200))

    _service().search("index=notable", earliest_time="-15m")

    body = create.calls.last.request.content.decode()
    assert "search=search+index%3Dnotable" in body
    assert "earliest_time=-15m" in body


@respx.mock
def test_search_returns_none_when_job_creation_fails():
    _mock_auth()
    respx.post(f"{BASE}/services/search/jobs").mock(
        return_value=httpx.Response(500, text="boom")
    )

    assert _service().search("index=notable") is None


@pytest.mark.parametrize("trailing", ["", "/"])
def test_server_url_trailing_slash_is_normalised(trailing):
    assert _service(server_url=BASE + trailing).server_url == BASE


# An agent writes its own SPL, so it writes the leading command too. Prepending a
# second one turned "search" into a keyword filter and returned a narrowed answer
# that looked like a real one.
class TestTheLeadingCommand:
    def test_leaves_a_query_that_already_searches_alone(self):
        from core.integrations.splunk.client import _as_search

        assert _as_search("search index=botsv3 sourcetype=stream:dns") == (
            "search index=botsv3 sourcetype=stream:dns"
        )

    def test_leaves_a_generating_command_alone(self):
        from core.integrations.splunk.client import _as_search

        assert _as_search("| tstats count where index=botsv3 by host") == (
            "| tstats count where index=botsv3 by host"
        )

    def test_adds_the_command_a_bare_query_is_missing(self):
        from core.integrations.splunk.client import _as_search

        assert _as_search("index=botsv3 error") == "search index=botsv3 error"

    def test_reads_the_command_whatever_its_case(self):
        from core.integrations.splunk.client import _as_search

        assert _as_search("SEARCH index=botsv3") == "SEARCH index=botsv3"
