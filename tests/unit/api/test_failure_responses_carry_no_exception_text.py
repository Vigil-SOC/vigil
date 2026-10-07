"""A failure inside an export or bulk write answers with a category, not the
exception text (database host, SQL, Jira URL, upstream body)."""

import httpx
import pytest

from services.api.errors import INTERNAL_ERROR_DETAIL
from services.api.routers import findings
from services.api.routers.jira_export import _export_failure

pytestmark = pytest.mark.unit

SECRET = "postgresql://app:hunter2@db.internal:5432"


def _status_error(code):
    request = httpx.Request("POST", "https://jira.internal.example/rest/api/2/issue")
    return httpx.HTTPStatusError(
        f"{SECRET} body",
        request=request,
        response=httpx.Response(code, request=request),
    )


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_status_error(401), "JIRA API error: HTTP 401"),
        (httpx.ConnectError(SECRET), "JIRA API error: could not reach Jira"),
        (RuntimeError(SECRET), INTERNAL_ERROR_DETAIL),
    ],
)
def test_jira_export_failure_is_a_category(exc, expected):
    result = _export_failure(exc)

    assert result.success is False
    assert result.error == expected


def test_bulk_enrich_reports_a_failed_finding_without_the_exception(monkeypatch):
    def _boom(finding_id):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(findings.data_service, "get_finding", _boom)
    request = findings.BulkEnrichmentRequest(finding_ids=["f-1"], enrichment_data={})

    result = findings.bulk_enrich_findings(request)

    assert result["results"]["failed"] == 1
    assert result["results"]["errors"] == [f"f-1: {INTERNAL_ERROR_DETAIL}"]
